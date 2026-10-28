"""LLM 없이 동작하는 규칙 기반 추출기.

목적: (1) API 키 없이 파이프라인 전체를 돌려보는 기준선(baseline), (2) LLM 장애 시 대체 경로.
LLM 제공자와 같은 인터페이스(`extract`)를 구현합니다. 이미지는 직접 못 읽으므로 OCR 텍스트를 받습니다.
"""
from __future__ import annotations

import re
import statistics

from . import taxonomy

SIDO = "서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주"
ADDR_RE = re.compile(
    rf"((?:{SIDO})[가-힣]*\s+[가-힣]+(?:시|군|구)(?:\s+[가-힣]+(?:구))?\s+[가-힣0-9]+(?:로|길)(?:\s?\d+(?:번)?길)?\s?\d+(?:-\d+)?(?:\s?\d+층)?)"
)
ADDR_LOOSE_RE = re.compile(rf"((?:{SIDO})[가-힣]*\s+[가-힣]+(?:시|군|구)\s+[가-힣0-9]+(?:동|읍|면|리))")
PHONE_RE = re.compile(r"(?<!\d)(0(?:2|[3-6]\d|70|50\d?)-\d{3,4}-\d{4})(?!\d)")  # 유선·인터넷·안심번호만 (휴대폰 제외)
HOURS_RE = re.compile(r"(\d{1,2}:\d{2}\s*[~\-–]\s*\d{1,2}:\d{2})")
PRICE_RE = re.compile(r"(?<![\d가-힣A-Za-z])([가-힣A-Za-z][가-힣A-Za-z ]{0,14}?)\s*[:\-]?\s*(\d{1,3}(?:,\d{3})+|\d{4,6}|\d{1,2}(?:\.\d)?만)\s*원")
HASHTAG_RE = re.compile(r"#([가-힣A-Za-z0-9_]+)")
MARKER_RE = re.compile(r"^\s*(?:📍|📌|🏠|(?:\d+[.)]\s*)?(?:상호명?|가게명?|장소|매장명?)\s*[:：])\s*(.+)$", re.M)
NUMBERED_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(.{2,30})$", re.M)
TAG_RE = re.compile(r"<(title|content|user_note)>\n?(.*?)\n?</\1>", re.S)

HOT_REGIONS = [
    "성수", "연남", "망원", "합정", "홍대", "익선", "을지로", "한남", "이태원", "압구정", "신사", "가로수길",
    "청담", "문래", "삼청", "서촌", "북촌", "잠실", "송리단길", "용산", "해방촌", "건대", "강남", "여의도",
    "해운대", "광안리", "전포", "애월", "성산", "협재", "경주", "강릉", "전주", "속초", "여수",
]

LABEL_LINE = re.compile(r"^\s*(?:주소|위치|영업시간|운영시간|휴무|전화|메뉴|가격|주차|예약)\s*[:：]", re.M)
TITLE_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|[:|]\s*네이버.*$|네이버 블로그|on Instagram.*$|후기|리뷰|내돈내산|추천|데이트|맛집|카페투어|솔직", re.I)


def _parse_user_text(user_text: str) -> dict:
    out = {"title": "", "content": "", "user_note": ""}
    for tag, body in TAG_RE.findall(user_text):
        out[tag] = body.strip()
    return out


def _split_name_branch(raw: str) -> tuple[str, str | None]:
    raw = re.split(rf"\s(?:{SIDO})[가-힣]*\s|[|/,(]|\s{{2,}}", raw.strip())[0].strip()
    raw = re.sub(r"[^\w\s가-힣&'.-]", "", raw).strip()
    m = re.match(r"^(.*?)\s*([가-힣A-Za-z0-9]+점)$", raw)
    if m and m.group(1) and len(m.group(2)) >= 3:
        return m.group(1).strip(), m.group(2)
    return raw, None


def _blocks(text: str) -> list[tuple[str, str]]:
    """(이름 후보, 블록 텍스트) 목록. 장소 표시(📍, '상호:')가 여러 개면 장소별로 나눕니다."""
    marks = list(MARKER_RE.finditer(text))
    if not marks:
        nums = [m for m in NUMBERED_RE.finditer(text) if not LABEL_LINE.match(m.group(0))]
        if len(nums) >= 2:
            marks = nums
    if not marks:
        return [("", text)]
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        start = m.start()
        # 표시 바로 앞의 짧은 소개 줄(예: 첫 줄의 지역/카테고리)도 첫 블록에 포함
        if i == 0:
            start = 0
        out.append((m.group(m.lastindex), text[start:end]))
    return out


_NOISE_LINE = re.compile(r"^[@#]|리뷰|영업|\d{1,2}[.:/]\d{1,2}|원$|^\W+$|오픈|추천|후기")


def _name_before_address(text: str) -> str | None:
    """장소 표시가 없는 캡쳐용: 주소 줄 바로 위의 '이름처럼 보이는' 줄을 상호명 후보로 씁니다."""
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if ADDR_RE.search(line) or ADDR_LOOSE_RE.search(line):
            window = lines[max(0, i - 4):i]
            ok = []
            for prev in window:
                core = re.sub(r"[^\w\s가-힣&'.-]", "", prev).strip()
                if not (2 <= len(core) <= 25) or _NOISE_LINE.search(prev):
                    continue
                if re.search(r"\s[|·]\s", prev):  # "전시 | 현대미술 기획전" 같은 분류·설명 줄
                    continue
                ok.append(core)
            if not ok:
                return None
            return ok[-1]  # 주소에 가장 가까운 이름 후보
    return None


# ---------------------------------------------------------------- 플레이스 화면(캡쳐 OCR) 전용 신호
PLACE_SCREEN_RE = re.compile(r"리뷰\s*[\d,]+|영업\s*(?:전|중|종료)|방문자\s*리뷰|블로그\s*리뷰")
RATING_RE = re.compile(r"([0-5]\.\d{1,2})\s*[·ㆍ・.:]?\s*(?:\(|리뷰|방문자|블로그|\d)")
REVIEW_RE = re.compile(r"(?:방문자\s*)?리뷰\s*([\d,]{1,7})")
STATION_RE = re.compile(r"([가-힣A-Za-z0-9]{1,12}역\s*(?:\d+\s*번\s*)?(?:출구)?\s*(?:에서)?\s*[\d,.]+\s*(?:m|km|미터))")
STAY_RE = re.compile(r"(\d+(?:\.\d)?)\s*시간\s*(?:정도\s*)?(?:소요|코스|걸려|이면)|(\d{2,3})\s*분\s*(?:정도\s*)?(?:소요|코스|테마|걸려)")
AMENITY_WORDS = ("단체", "화장실", "무선 인터넷", "와이파이", "포장", "배달", "예약", "반려", "유아", "콜키지", "주차", "발렛",
                 "남/녀", "노키즈", "휠체어", "대기공간", "흡연")
HOURS_LINE_RE = re.compile(r"\d{1,2}:\d{2}|\d{1,2}\s*시|휴무|브레이크|라스트|매일|연중무휴|24시간")
HOURS_KEY_RE = re.compile(r"영업|운영|오픈|마감|휴무|브레이크|라스트|L\.?O|매일|평일|주말|[월화수목금토일]요일|[월화수목금토일]\s*[~\-]\s*[월화수목금토일]")


def _place_screen_name(text: str) -> str | None:
    """네이버/카카오 플레이스 화면 캡쳐: 맨 위 몇 줄 중 이름처럼 보이는 첫 줄."""
    if not PLACE_SCREEN_RE.search(text):
        return None
    from .ocr import _clean_name_line, _name_line

    lines = [l.strip() for l in text.splitlines()]
    ni = _name_line(lines)
    if ni is not None:  # '업종 · ★평점 · 리뷰' 줄 바로 위 = 상호명. 깨진 줄이면 이름을 만들지 않음(엉뚱한 줄 방지)
        core = re.sub(r"[^\w\s가-힣&'.-]", "", lines[ni]).strip(" -_.")
        return core if _clean_name_line(lines[ni]) and 2 <= len(core) <= 30 else None
    for line in [l.strip() for l in text.splitlines() if l.strip()][:5]:
        core = re.sub(r"[^\w\s가-힣&'.-]", "", line).strip(" -_.")
        core = re.sub(r"\s+[A-Za-z=]{1,2}$", "", core).strip()
        if 2 <= len(core) <= 25 and re.search(r"[가-힣]", core) and not re.search(r"리뷰|영업|\d{2,}|쿠폰|예약|출발|도착", core):
            return core
    return None


def _extras(block: str, menus: list[dict], primary: str) -> dict:
    """영업시간·평점·역·편의·예약·체류시간 등 v0.2 필드."""
    out: dict = {}
    lines = [l.strip(" ·ㆍv") for l in block.splitlines() if l.strip()]
    hours_lines = []
    for l in lines:
        if HOURS_LINE_RE.search(l) and HOURS_KEY_RE.search(l) and not re.search(r"원\b|\d+원", l):
            hours_lines.append(re.sub(r"^(?:영업|운영)\s*(?:시간)?\s*[:：]?\s*(?:(?:영업\s*)?(?:전|중|종료)\s*[·ㆍ]\s*)?", "", l))
    if hours_lines:
        out["opening_hours_text"] = " / ".join(dict.fromkeys(hours_lines))[:200]
    m = REVIEW_RE.search(block)
    if m:
        out["review_count"] = int(m.group(1).replace(",", ""))
    m = RATING_RE.search(block)
    if m and (out.get("review_count") or "★" in block or "별점" in block or "평점" in block):
        out["rating"] = float(m.group(1))
    m = STATION_RE.search(block)
    if m:
        out["nearest_station_text"] = re.sub(r"\s+", " ", m.group(1)).strip()
    park = next((l for l in lines if "주차" in l or "발렛" in l), None)
    if park:
        out["parking_text"] = re.sub(r"(\.\.\.|…).*$|\s+[v\d\s]{1,6}$", "", park).strip()[:60]
    am = []
    for l in lines:
        if l == park:
            continue
        if sum(w in l for w in AMENITY_WORDS) >= 2 and "," in l:
            am += [a.strip(" .…") for a in re.split(r"[,·]", l) if a.strip(" .…")]
    out["amenities"] = list(dict.fromkeys(a for a in am if 1 < len(a) <= 20))[:8]
    low = block.lower()
    if "catchtable" in low or "캐치테이블" in block:
        out["reservation_method"] = "catchtable"
    elif "booking.naver" in low or "n예약" in low or "네이버 예약" in block or "네이버예약" in block:
        out["reservation_method"] = "naver"
    elif re.search(r"전화\s*예약", block):
        out["reservation_method"] = "phone"
    elif re.search(r"DM\s*예약|디엠", block, re.I):
        out["reservation_method"] = "dm"
    elif re.search(r"예약\s*(?:불가|안\s*받)|워크인", block):
        out["reservation_method"] = "walk_in"
    m = STAY_RE.search(block)
    if m:
        out["stay_minutes_hint"] = int(float(m.group(1)) * 60) if m.group(1) else int(m.group(2))
    if menus:
        prices = sorted(x["price"] for x in menus if x.get("price"))
        if prices:
            med = statistics.median(prices)
            factor = {"restaurant": 1.2, "bar": 2.0, "cafe": 1.3}.get(primary, 1.0)
            out["est_cost_per_person"] = int(round(med * factor, -3)) or int(med)
    return out


def _price_to_int(s: str) -> int:
    if "만" in s:
        return int(float(s.replace("만", "")) * 10000)
    return int(s.replace(",", ""))


def _line_with(text: str, keys: tuple[str, ...], drop: tuple[str, ...] = ()) -> str | None:
    """키워드가 있는 줄에서, '/'·'|' 로 나뉜 조각 중 그 키워드가 든 부분만 돌려줍니다."""
    for line in text.splitlines():
        if not any(k in line for k in keys):
            continue
        for piece in re.split(r"\s[/|]\s|\s?/\s?(?=\D)", line):
            if any(k in piece for k in keys) and not any(d in piece for d in drop):
                v = re.split(r"[:：]", piece, maxsplit=1)
                val = (v[1] if len(v) > 1 else piece).strip(" -·")
                if val:
                    return val[:80]
    return None


def _place_screen_fallback(text: str) -> str | None:
    """플레이스 캡쳐인데 상호명 줄이 깨진 경우(예: 'APSARA] 대학로점'): 그 줄의 지점명이나
    '업종 · 평점' 줄의 업종으로 임시 이름을 만듭니다. 실제 장소는 후보 확인 단계에서 주소로 찾습니다."""
    if not PLACE_SCREEN_RE.search(text):
        return None
    from .ocr import _name_line

    lines = [l.strip() for l in text.splitlines()]
    ni = _name_line(lines)
    if ni is None:
        return None
    m = re.search(r"([가-힣A-Za-z0-9]{1,10}점)\s*$", lines[ni])
    if m and len(m.group(1)) >= 3:
        return m.group(1)
    cat = re.match(r"\s*([가-힣]{2,10})\s*[·ㆍ・]", lines[ni + 1] if ni + 1 < len(lines) else "")
    return cat.group(1) if cat else None


def _one(name_raw: str, block: str, title: str, title_is_name: bool = False) -> dict | None:
    evidence = []
    if name_raw:
        name, branch = _split_name_branch(name_raw)
        evidence.append(name_raw.strip()[:60])
        conf = 0.55
    elif title and title_is_name:  # 네이버 플레이스 og:title = 정식 상호명
        name, branch = _split_name_branch(title)
        conf = 0.6
        evidence.append(title[:60])
    elif (guess := _place_screen_name(block)):
        name, branch = _split_name_branch(guess)
        conf = 0.55
        evidence.append(guess[:60])
    elif (guess := _place_screen_fallback(block)):
        name, branch = guess, None
        conf = 0.25
        evidence.append("상호명 인식 불확실: " + guess)
    elif (guess := _name_before_address(block)):
        name, branch = _split_name_branch(guess)
        conf = 0.45
        evidence.append(guess[:60])
    elif title:
        name, branch = _split_name_branch(TITLE_NOISE.sub(" ", title))
        conf = 0.35
        evidence.append(title[:60])
    else:
        return None
    if not name or len(name) < 2:
        return None

    addr_m = ADDR_RE.search(block) or ADDR_LOOSE_RE.search(block)
    address = addr_m.group(1).strip() if addr_m else None
    if address:
        conf += 0.2
        evidence.append(address[:60])
    phone_m = PHONE_RE.search(block)
    if phone_m:
        conf += 0.05

    region = next((r for r in HOT_REGIONS if r in block or r in title), None)
    if not region and address:
        gu = re.search(r"([가-힣]+(?:구|군|시))\s", address)
        region = gu.group(1) if gu else None

    menus = []
    for mname, mprice in PRICE_RE.findall(block):
        mname = mname.strip()
        if mname and not any(w in mname for w in ("가격", "인당", "총", "합계", "주차")):
            menus.append({"name": mname, "price": _price_to_int(mprice)})
    menus = list({m["name"]: m for m in menus}.values())[:5]  # 같은 캡쳐를 여러 번 올려도 중복 집계 안 함
    price_level = None
    if menus:
        med = statistics.median(m["price"] for m in menus)
        price_level = 1 if med < 10000 else 2 if med < 30000 else 3 if med < 60000 else 4

    # 플레이스 화면의 '업종 · 평점 · 리뷰' 줄(예: "일식당 · 4.88 · 리뷰 539")을 우선 사용, 지점명('도산공원점')은 분류에서 제외
    cat_line = re.search(r"^\s*([가-힣A-Za-z,/ ]{2,15}?)\s*[·ㆍ]\s*.*?(?:리뷰|★|\d\.\d)", block, re.M)
    cls_block = block.replace(branch, " ") if branch else block
    cat = taxonomy.classify_text(name, cat_line.group(1) if cat_line else None, cls_block, title) or ("other", "other")
    tags = taxonomy.tags_from_text(block)
    free = [h for h in dict.fromkeys(HASHTAG_RE.findall(block)) if h not in name][:5]

    hours = HOURS_RE.search(block)
    cautions = []
    if "웨이팅" in block:
        cautions.append("웨이팅 있음")
    if "주차" in block and ("불가" in block or "없" in block):
        cautions.append("주차 어려움")

    extras = _extras(block, menus, cat[0])
    rec = {
        "name": name, "branch": branch, "address_hint": address, "region_hint": region,
        "phone": phone_m.group(1) if phone_m else None,
        "primary_category": cat[0], "sub_category": cat[1],
        "tags": tags, "free_tags": free, "signature_menus": menus,
        "price_level": price_level, "price_text": None,
        "opening_hours_text": _line_with(block, ("영업시간", "운영시간"), drop=("휴무",)) or (hours.group(1) if hours else None),
        "closed_days_text": _line_with(block, ("휴무",)),
        "reservation_text": _line_with(block, ("예약 필수", "예약필수", "예약제", "예약 가능", "예약 불가", "전화 예약",
                                                "3일 전 예약", "예약 마감", "캐치테이블")),
        "summary": None, "highlights": [], "cautions": cautions,
        "evidence": evidence, "confidence": round(min(conf, 0.8), 2),
    }
    # v0.2 필드: 기존 값이 없을 때만 채움 (영업시간은 더 풍부한 쪽 우선)
    for k, v in extras.items():
        if v in (None, [], ""):
            continue
        if k == "opening_hours_text" and rec.get(k) and len(rec[k]) >= len(v):
            continue
        rec[k] = v
    return rec


# ---------------------------------------------------------------- 해시태그 (장소 표시가 없는 인스타 캡션)
# 예: "#잠실맛집 #송리단길 #쿠차라 #배키욘방" → 지역 단서 '잠실', 장소 후보 쿠차라·배키욘방
_GENERIC_TAG = re.compile(
    r"맛집|추천|데이트|코스|투어|핫플|여행|먹스타|맛스타|먹방|일상|데일리|스타그램|그램|순례|가볼만한|나들이|모음|리스트|"
    r"베스트|top|insta|food|daily|ootd|love|like|follow|selfie|reels?$", re.I)
_CATEGORY_WORDS = ("카페", "술집", "식당", "밥집", "음식점", "디저트", "브런치", "빵집", "베이커리", "와인바", "바", "펍",
                   "이자카야", "전시", "팝업", "놀거리", "역", "동", "길", "")
HASHTAG_MAX = 10


def _hashtag_places(text: str, title: str) -> list[dict]:
    """장소 표시가 하나도 없을 때만: 지역·일반 태그를 빼고 남은 해시태그를 장소 이름 후보로."""
    tags = list(dict.fromkeys(HASHTAG_RE.findall(f"{title}\n{text}")))
    regions: list[str] = []
    names: list[str] = []
    for t in tags:
        region = next((r for r in HOT_REGIONS if r in t), None)
        rest = t.replace(region, "", 1) if region else t
        if region:
            regions.append(region)
        if _GENERIC_TAG.search(t) or (region and rest in _CATEGORY_WORDS) or len(t) < 2 or t.isdigit():
            continue
        names.append(t)
    if not names:
        return []
    region = max(dict.fromkeys(regions), key=regions.count) if regions else None  # 가장 많이 나온 지역 (동률이면 먼저 나온 것)
    out = []
    for name in names[:HASHTAG_MAX]:
        p = _one(name, name, "")
        if not p:
            continue
        p["region_hint"] = region
        p["free_tags"] = []
        p["confidence"] = 0.35
        p["evidence"] = ["해시태그에서 찾은 이름: " + name] + ([f"지역 단서: {region}"] if region else [])
        out.append(p)
    return out


class HeuristicExtractor:
    name = "heuristic"
    supports_vision = False

    def extract(self, system: str, user_text: str, images, schema: dict) -> dict:
        parts = _parse_user_text(user_text)
        text = "\n".join(p for p in (parts["content"], parts["user_note"]) if p)
        title = parts["title"]
        title_is_name = 'type="naver_place"' in user_text
        places = []
        for name_raw, block in _blocks(text):
            # 제목으로 이름을 짐작하는 건 해시태그까지 본 뒤에 (인스타 og:title "Instagram의 ○○님: …" 은 상호명이 아님)
            p = _one(name_raw, block, title if (title_is_name and not name_raw) else "", title_is_name)
            if p and all(p["name"] != q["name"] for q in places):
                places.append(p)
        if not places:  # 장소 표시가 없고 해시태그로만 가게를 적은 캡션
            places = _hashtag_places(text, title)
        if not places and title:
            p = _one("", text, title, title_is_name)
            if p:
                places.append(p)
        note = parts["user_note"].strip()
        if not places and note and "\n" not in note and len(note) <= 30:
            # 링크에서 못 읽어 사용자가 상호명만 적어 준 경우
            p = _one(note, note, "")
            if p:
                p["confidence"] = 0.45
                places.append(p)
        return {"places": places, "is_place_content": bool(places), "notes": "heuristic"}
