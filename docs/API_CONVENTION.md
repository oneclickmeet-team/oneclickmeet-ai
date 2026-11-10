# OneClickMeet AI 워크플로우 v2.1 — AI 내부 API 및 코드 컨벤션

> **범위:** Spring Boot → FastAPI **AI 내부 API**만 다룬다(장소 후보·인식·코스 추천). 회원·보관함·코스 저장·공유는 Spring Boot 소관이다(TRD 부록 A).
> - 기준 문서: 원클릭밋 TRD v1.0 (2026-10-18). 앱이 부르는 공개 API(`/api/v1`)는 TRD 부록 A를 따른다.
> - 구현: AI 로직 `placeflow/v3/` · AI 내부 서버 `placeflow/ai_api/` (Python 3.10+, FastAPI, pydantic v2)
> - 실행: `python -m placeflow api --port 8001` → Swagger `http://127.0.0.1:8001/docs`, 명세 파일 `docs/openapi.json`
> - "V3"는 Figma 와이어프레임 버전이고, 워크플로우 버전은 **v2.1**이다.

---

## 0. 구조

```
[React Native] ──/api/v1──▶ [Spring Boot]  인증·인가·업무 데이터(MySQL)
                                 │  저장 장소를 요청에 담아 보냄
                                 └──/internal/v1──▶ [FastAPI AI]  placeflow.ai_api → placeflow.v3
                                                       ├─ 장소 검색: 카카오 로컬 (고정)
                                                       ├─ 장소 태그: 네이버 블로그 검색 제목·요약 → OpenAI 가 태그 12개 중 판단
                                                       ├─ 인식: 링크 메타·공유 텍스트·스크린샷(OCR → 실패 시 비전 LLM) → 후보 매칭
                                                       └─ 추천: 카테고리 순서 + 필수 장소 위치 → 도보 코스
```
- **상태 없음:** FastAPI는 DB에 쓰지 않고 사용자 데이터를 보관하지 않는다(TRD 4.2). 업로드 이미지는 처리 후 바로 지운다.
- **추천은 저장이 아니다:** 결과의 `candidateKey`는 이번 응답에서만 쓰는 값이고 영구 `courseId`가 아니다. 저장은 Spring의 T02에서 한다.

### Spring(공개 API) ↔ FastAPI(내부 API) 대응

| TRD | 앱 → Spring | Spring → FastAPI | 비고 |
|---|---|---|---|
| P01 | `GET /api/v1/regions` | `GET /internal/v1/regions` | |
| P02 | `GET /api/v1/place-candidates` | `GET /internal/v1/place-candidates` | 키워드 또는 좌표 |
| P03 | `POST /api/v1/place-recognitions` | `POST /internal/v1/place-recognitions` | Spring이 `savedPlaces` 추가 가능 |
| P04 | `POST /api/v1/place-image-recognitions` | `POST /internal/v1/place-image-recognitions` | multipart 그대로 전달 |
| R01 | `POST /api/v1/course-recommendations` | `POST /internal/v1/course-recommendations` | Spring이 기준 장소·저장 장소 정보를 채워 보냄 |
| (확장) | — | `POST /internal/v1/place-candidates/replacements` | 코스 편집 교체 후보 |
| (확장) | — | `GET /internal/v1/categories` | 카테고리 코드 7개 |
| — | — | `GET /health` | 운영 확인 (인증 없음) |

---

## 1. 공통 계약 (TRD 부록 A 2와 같음)

| 항목 | 규칙 |
|---|---|
| 경로 | `/internal/v1` |
| 형식 | JSON **camelCase**. 요청은 snake_case도 받지만 정식 표기는 camelCase |
| 성공 | 래퍼 없이 **DTO를 그대로** 반환한다. 모든 엔드포인트가 200(조회·계산) |
| 오류 | `application/problem+json` (ProblemDetail + `code`·`reason`) |
| 이미지 | P04만 `multipart/form-data` |
| 값 없음 | `null`, 빈 목록은 `[]` |
| 인증 | `AI_SERVICE_KEY`를 설정하면 `X-AI-Key` 헤더가 필수다(없으면 401). 미설정은 로컬 개발용 |
| 타임아웃(권장) | Spring → FastAPI: P03·P04 30초, R01 15초, 나머지 10초. 재시도는 P01·P02만 1회. 비전 LLM 폴백을 켜면 P04는 30초를 넘길 수 있다(38건 평가에서 최대 약 60초) |

### 1.1 오류 응답
```json
{ "type": "about:blank", "title": "Service Unavailable", "status": 503,
  "detail": "외부 장소 검색 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
  "instance": "/internal/v1/course-recommendations", "code": "EXTERNAL-0503", "reason": "PROVIDER_UNAVAILABLE" }
```

| status | code / reason | 언제 |
|---|---|---|
| 400 | `COMMON-0400` / `INVALID_INPUT` | 필수값·형식·범위 오류. `errors: [{field, message}]` 포함 |
| 401 | `AUTH-0401` / `INVALID_SERVICE_KEY` | `X-AI-Key` 없음·불일치 |
| 404 | `COMMON-0404` / `RESOURCE_NOT_FOUND` | 필수 장소 정보가 요청에 없음 |
| 413 | `PLACE-0413` / `IMAGE_TOO_LARGE` | 이미지 장당 5MiB 초과 (`AI_MAX_IMAGE_MB`) |
| 415 | `PLACE-0415` / `UNSUPPORTED_IMAGE_TYPE` | JPEG·PNG·WebP가 아님 (파일 앞부분으로 판정) |
| 503 | `EXTERNAL-0503` / `PROVIDER_UNAVAILABLE` | 카카오 장소 검색 실패·한도 소진 (네이버 블로그·OpenAI 태그 실패는 503 이 아니라 태그만 빈 목록) |
| 503 | `EXTERNAL-0503` / `RECOGNIZER_UNAVAILABLE` | 서버에 OCR(tesseract+kor)과 비전 LLM이 모두 없음, 또는 OCR 오류 |
| 500 | `COMMON-0500` / `INTERNAL_ERROR` | 예상하지 못한 오류 |

- 외부 제공자 오류 원문과 내부 예외는 응답에 넣지 않고 서버 로그에만 남긴다.
- **정상 분기는 오류가 아니다**: 인식 불일치는 `200 + NEEDS_SEARCH`, 추천 결과 없음은 `200 + courses: [] + reason`.

---

## 2. 공통 DTO

### SavedPlaceIn — Spring이 보내는 저장 장소
```json
{ "placeId": 41, "source": "PERSONAL", "name": "연남 감성 카페", "address": "서울 마포구 연남로 1",
  "latitude": 37.5631, "longitude": 126.9282, "categoryCode": null }
```
- `placeId` 하나로 식별한다. 공동 장소도 이 값 하나로 보내고, `source: "SHARED"`로 구분한다.
- `categoryCode`가 없으면 **AI가 추론한다**(현재 TRD의 `place_records`에 카테고리가 없음 → 방법 b).
  1. 장소 검색으로 같은 가게(이름 일치 + 200m 이내)를 찾아 그 분류를 쓴다.
  2. 못 찾으면 이름 글자로 분류한다(예: "○○ 카페" → CAFE).
  3. 그래도 모르면 카테고리 자리에는 못 들어간다. 기준 장소로는 쓸 수 있다.
  - DB에 `categoryCode`를 저장하게 되면(방법 a) 그 값을 보내면 된다.

### PlaceCandidate — 저장 전 확인용 후보 (TRD CandidateSearchResponse.items)
```json
{ "name": "오지다", "address": "서울특별시 송파구 백제고분로48길 24", "latitude": 37.5131, "longitude": 127.1123,
  "sourceUrl": "http://place.map.kakao.com/123456789",
  "provider": "KAKAO", "providerPlaceId": "123456789",
  "roadAddress": "서울특별시 송파구 백제고분로48길 24", "jibunAddress": "서울 송파구 방이동 44-1",
  "categoryLabel": "맛집", "categoryCode": "MEAL",
  "openingHours": null, "distanceMeters": 120, "savedPlaceId": null }
```
- 후보는 `placeId`를 갖는 저장 본문이 아니다. 사용자가 고른 후보의 `providerPlaceId`·`name`·`roadAddress`·`jibunAddress`·`latitude`·`longitude`·`sourceUrl`을 Spring 보관함 저장에 전달한다.
- `provider`·`providerPlaceId` (2026-11-06 추가): 장소 정보 제공자(`KAKAO` · 데모 데이터는 `DEMO`)와 제공자 장소 ID(카카오 장소 `id`). Spring 보관함의 저장 키다. 링크에서만 읽고 검색으로 확인하지 못한 장소는 둘 다 null이다.
- `roadAddress`·`jibunAddress` (2026-11-06 추가): 도로명·지번 주소를 나눠 준다. 없는 쪽은 null. `address`는 표시용 한 줄(도로명, 없으면 지번)로 그대로 남겨 둔다.
- `openingHours`는 확보했을 때만 들어간다. 없으면 앱이 "운영시간 확인 필요"를 안내한다.
- `distanceMeters`는 좌표 검색일 때만, `savedPlaceId`는 (확장) 요청에 `savedPlaces`를 보냈을 때만 채워진다. `savedPlaceId`는 이름이 같고 80m 이내인 저장 장소의 `placeId`다.

### 카테고리 코드 (v2 확정 7개)

| 코드 | 범위 | 카카오 그룹 |
|---|---|---|
| `MEAL` | 음식점 (술집 제외) | FD6 |
| `CAFE` | 카페·디저트 | CE7 |
| `BAR` | 술집·바·이자카야 | (키워드) |
| `ACTIVITY` | 방탈출·보드게임·공방·포토부스 등 체험 | (키워드) |
| `CULTURE` | 전시·공연·미술관·도서관 | CT1 |
| `WALK` | 공원·산책로·전망·관광명소 | AT4 |
| `SHOPPING` | 소품샵·편집숍·시장 | (키워드) |

숙박·기타는 코스 후보에서 빠진다.

---

## 3. 엔드포인트

### 3.1 P01 지원 지역 — `GET /internal/v1/regions`
```json
{ "items": [ { "regionId": "YEONNAM", "name": "연남", "latitude": 37.5626, "longitude": 126.9255, "radiusMeters": 1200 } ] }
```
- `regionId`는 `JAMSIL`·`SUNGSHIN`·`HANSUNG`·`HYEHWA`·`SEOCHON`·`YEONNAM` 중 하나다.
- 중심 좌표·반경은 근사값이다(경계는 후속 확정).

### 3.2 P02 장소 후보 검색 — `GET /internal/v1/place-candidates`

| 쿼리 | 기본 | 설명 |
|---|---|---|
| `query` | — | 키워드. 좌표가 없으면 필수 |
| `latitude`·`longitude` | — | 함께 보낸다. 주변 놀거리·교체 후보 검색용 (선택한 장소의 좌표, 사용자 실시간 위치 아님) |
| `regionId` | — | 좌표가 없으면 지역 중심 좌표로 검색 |
| `radiusMeters` | 1000 | 1~3000 |
| `categoryCode` | — | (확장) 좌표 검색을 카테고리로 제한 |
| `page` / `size` | 0 / 15 | `size` 최대 30 |

- 응답은 `{ "items": [PlaceCandidate], "page": 0, "size": 15, "hasNext": false }`이다.
- 키워드와 좌표를 함께 보내면 그 좌표 주변에서 키워드를 검색한다.
- 키워드만 보낸 검색은 카카오 한 번 결과까지다(15건).
- 외부 검색이 실패하면 503이다.

### 3.3 P03 링크·텍스트 인식 — `POST /internal/v1/place-recognitions`
```json
{ "sourceUrl": "https://www.instagram.com/reel/DegiE1ozc74/",
  "sharedText": "1. 오지다 (서울 송파구 백제고분로48길 24)\n2. 닭튜브 ...",
  "savedPlaces": [ { "placeId": 7, "name": "닭튜브 방이점", "latitude": 37.5131, "longitude": 127.1123 } ] }
```
- `sourceUrl`·`sharedText` 중 하나 이상이 필수다.
- `sharedText`에 앱 '공유 → 복사' 문구를 통째로 넣어도 된다. 첫 링크를 `sourceUrl`로 쓰고 나머지를 장소 단서로 쓴다.
- **링크는 robots.txt를 지키며 메타 정보(og:title·og:description)만 읽는다.** 본문·가격·메뉴는 수집하지 않는다(TRD).
- 네이버 지도처럼 robots.txt가 막은 링크와 로그인 벽이 있는 인스타는 공유 텍스트·캡션으로 진행한다.
- 링크에서는 이미지를 읽지 않고 비전 LLM 폴백도 쓰지 않는다. **사진에만 장소가 있는 인스타 게시물은 P03으로 찾을 수 없고**, 캡쳐를 P04로 보내야 한다.
- 캡션에 번호 목록·📍 같은 장소 표시가 없고 가게가 해시태그로만 있으면(`#잠실맛집 #쿠차라 #배키욘방`), 일반·지역 태그를 뺀 해시태그를 장소 이름으로 쓰고 지역 태그(→ 잠실)를 검색어에 붙인다. 인스타 고정 댓글은 로그인이 필요해 읽지 않는다(앱에서 복사해 `sharedText`로 보내야 한다).

응답:
```json
{
  "status": "CANDIDATES_FOUND",
  "extractedQuery": "오지다",
  "candidates": [ { "name": "오지다", "...": "PlaceCandidate", "matchType": "EXACT", "matchReasons": ["주소 일치", "이름 일치"],
                   "tags": [ { "label": "음식이 맛있어요", "mentionCount": 12 }, { "label": "양이 많아요", "mentionCount": 5 } ],
                   "tagPostCount": 31 } ],
  "sourceUrl": "https://www.instagram.com/reel/DegiE1ozc74/",
  "message": null,
  "groups": [
    { "order": 1, "extractedName": "오지다", "extractedAddress": "서울 송파구 백제고분로48길 24", "status": "MATCHED",
      "best": { "name": "오지다", "matchType": "EXACT", "matchReasons": ["주소 일치", "이름 일치"], "...": "" },
      "others": [ { "name": "회포차오지다", "matchType": "SIMILAR", "matchReasons": ["이름 일부만 같아요", "주소 다름"], "...": "" } ] }
  ],
  "tagSource": "NAVER_BLOG"
}
```

| 필드 | 값 | 앱 처리 |
|---|---|---|
| `status` | `CANDIDATES_FOUND` / `NEEDS_SEARCH` | `NEEDS_SEARCH`: 직접 검색 화면, 검색창에 `extractedQuery` |
| `candidates` | 모든 묶음의 best → others 순 | TRD의 후보 목록 |
| `groups` (확장) | 링크에 나온 장소 1곳 = 묶음 1개 (릴스 '맛집 4곳') | 장소별 묶음 + 여러 곳 저장 화면 |
| `matchType` | `EXACT`(확실한 일치) / `SIMILAR`(비슷한 이름) | **EXACT만 기본 선택**. SIMILAR는 접어서 이유와 함께 |
| `groups[].status` | `MATCHED` · `SIMILAR_ONLY` · `NOT_FOUND` | `NOT_FOUND`: "찾지 못했어요 · 직접 검색" |
| `tags` (확장) | 해당 블로그 글이 많은 순 최대 5개 `{label, mentionCount}` | 저장할 때 장소 태그로 보관. "블로그 언급 기반" 표시 |
| `tagPostCount` (확장) | OpenAI 에 보낸 블로그 글 수 (가게 이름이 나온 글) | **3 미만이면 AI 서버가 `tags`를 `[]`로 내려 준다**(2026-11-10). 백엔드는 받은 `tags`를 그대로 쓰면 된다 |
| `tagSource` (확장) | `NAVER_BLOG` / null | null: 태그 기능 꺼짐(네이버·OpenAI 키 없음) 또는 블로그 검색·OpenAI 판단이 모두 실패 |

**장소 태그** (`placeflow/v3/blog_tags.py`, 태그 목록 `placeflow/v3/review_tags.json`)
- 태그는 팀이 정한 **12개**이고 모든 카테고리에 공통으로 쓴다: 뷰가 좋아요 · 컨셉이 독특해요 · 사진이 잘나와요 · 차분한 분위기에요 · 야외공간이 멋져요 · 아늑해요 · 혼술하기 좋아요 · 집중하기 좋아요 · 음식이 맛있어요 · 양이 많아요 · 특별한 메뉴가 있어요 · 디저트가 맛있어요. 응답의 `label`은 이 문자열 그대로다. 목록과 LLM 용 설명(`hint`)은 `review_tags.json`에서 고친다.
- '가게 이름 + 동네'로 네이버 블로그 검색(최대 50건, 정확도순) → 제목·요약에 가게 이름이 나온 글만 → **OpenAI(기본 `gpt-4o-mini`)가 글마다 그 가게를 긍정적으로 말하는 태그를 12개 중에서 고른다**(부정·다른 가게 이야기는 제외, JSON 스키마의 enum 으로 목록 밖 태그 차단) → 태그별 글 수를 세어 상위 5개. 장소 1곳당 OpenAI 1회 호출.
- OpenAI 호출이 실패하면 규칙 기반으로 대신하지 않고 태그 없이 응답한다.
- **블로그 본문은 읽지 않는다**(TRD). 네이버 플레이스의 실제 리뷰 선택 횟수가 아니라 **블로그 언급 수**다.
- 우리 앱 사용자가 고른 태그 집계(B 단계)는 Spring이 DB로 처리하고, 언제 사용자 집계로 바꿔 보여줄지도 Spring이 정한다. FastAPI는 블로그 기반 태그만 만든다.
- P04 스크린샷 인식에도 같은 필드가 붙는다. 같은 가게는 메모리 캐시 24시간(OpenAI 재호출 없음), 한 요청에서 최대 12곳까지 태그를 붙인다.

**판정 규칙** (`save_flow.match`)
- **EXACT**
  - 도로명+건물번호가 같고 이름도 비슷하다.
  - 또는 공백·기호를 뺀 이름이 같다("○○점" 지점명 허용). 단, 주소가 다르면 안 된다.
- **SIMILAR**
  - 이름 일부만 같다(오지다 ⊂ 회포차오지다).
  - 철자가 비슷하다(0.75 이상).
  - 같은 이름이지만 다른 지점이다.
  - 주소가 없고, 지역 단서(`혜화`·`잠실` 등)와 다른 구에 있다. 이유는 "다른 지역"이다. LLM이 읽은 주소가 지역 단서와 다른 구이면 그 주소는 쓰지 않는다.
  - 주소만 같고 이름이 다르다. 이유는 "주소만 일치"다.
- 주소가 같고 이름 유사도가 0.5 이상이면 오독으로 보고 EXACT로 판정한다(가우뚱 → 가우동).
- 주소가 없으면 캡션 속 상가·건물 이름과 같은 링크 장소들이 모인 구를 보조 근거로 쓴다.
- 링크 하나에서 최대 10곳까지 인식한다.

### 3.4 P04 스크린샷 인식 — `POST /internal/v1/place-image-recognitions`
`multipart/form-data`

| 필드 | 필수 | 설명 |
|---|---|---|
| `image` | O | JPEG·PNG·WebP, 장당 최대 5MiB |
| `extraImages` | | (확장) 같은 장소의 추가 화면 최대 4장(홈·메뉴·정보 탭). 사진마다 다른 장소가 있는 게시물용이 아니다 |
| `sharedText` | | (확장) 함께 붙여넣은 캡션 |

- 응답은 P03과 같은 `RecognitionResponse`다(`sourceUrl`은 null).
- **OCR**: Tesseract `kor+eng`로 약 1500px까지 확대한 뒤 전처리 3가지로 동시에 읽고, 가장 좋은 결과를 고른다.
  - 상호명이 깨지면 다른 결과에서 가져온다. 그래도 안 되면 정확히 읽힌 주소로 검색한다.
  - 동시 처리는 `AI_OCR_CONCURRENCY`(기본 2)개까지다. 비전 LLM을 기다리는 동안에도 이 슬롯을 차지한다.
- **비전 LLM 폴백** (`AI_VISION_FALLBACK`, 기본 끔): OCR + 규칙 기반으로 먼저 추출하고, 다음 경우에만 이미지를 비전 LLM으로 보내 다시 추출한다.
  - 장소 없음 / 확신도 0.5 미만 / 캡쳐마다 다른 상호 / OCR 오류
  - LLM 호출이 실패하면 OCR 결과가 있을 때는 그 결과를 쓴다. OCR 결과도 없으면 503이 아니라 `200 + NEEDS_SEARCH`다.
  - LLM이 "장소 없음"으로 판단하면 `200 + NEEDS_SEARCH`다.
- `AI_LLM`을 비전 LLM으로 설정하면 OCR을 건너뛰고 처음부터 LLM으로 읽고, 링크·텍스트 추출도 LLM으로 바뀐다.

```python
# Spring 쪽 예시는 아니고, 흐름 확인용 (httpx)
httpx.post(f"{AI}/internal/v1/place-image-recognitions", headers={"X-AI-Key": KEY},
           files=[("image", ("cap.png", data, "image/png"))], timeout=30)
```

### 3.5 R01 코스 추천 — `POST /internal/v1/course-recommendations`
```json
{
  "anchor": { "type": "PLACE", "placeId": 41 },
  "placeCount": 4,
  "categorySequence": ["MEAL", "CAFE", "ACTIVITY", "BAR"],
  "requiredPlaceOrder": 2,
  "anchorPlace": { "placeId": 41, "source": "PERSONAL", "name": "연남 감성 카페", "latitude": 37.5631, "longitude": 126.9282 },
  "savedPlaces": [ { "placeId": 12, "source": "PERSONAL", "name": "연남 파스타집", "latitude": 37.5626, "longitude": 126.9255 },
                   { "placeId": 81, "source": "SHARED",   "name": "연남 와인바",   "latitude": 37.5667, "longitude": 126.9279 } ],
  "excludeCandidateKeys": []
}
```

| 필드 | 필수 | 규칙 |
|---|---|---|
| `anchor` | O | `{type: "REGION", regionId}` 또는 `{type: "PLACE", placeId}` |
| `placeCount` | O | 3~5 |
| `categorySequence` | O | 카테고리 코드 7개 중에서, 원소 수 = `placeCount` |
| `requiredPlaceOrder` | PLACE면 O | 1~`placeCount`. REGION이면 보내지 않는다(보내면 400) |
| `anchorPlace` | | 필수 장소 정보. 없으면 `savedPlaces`에서 `placeId`로 찾는다. 어디에도 없으면 404 |
| `savedPlaces` | | 추천 재료(최대 500곳). 본인 개인 장소 + Spring이 `sourceCoupleId`로 고른 공동 장소. 상대의 비공개 보관함은 넣지 않는다 |
| `excludeCandidateKeys` | | (확장) 다시 추천받기: 앞에서 받은 `candidateKey` 전부 |

응답:
```json
{
  "courses": [ {
      "candidateKey": "crs_c68221c0fc",
      "title": "연남 맛집·카페·놀거리·술집 코스",
      "places": [
        { "visitOrder": 1, "name": "연남 파스타집", "address": "...", "latitude": 37.5626, "longitude": 126.9255,
          "categoryCode": "MEAL", "categoryLabel": "맛집", "source": "PERSONAL", "placeId": 12, "isAnchor": false, "sourceUrl": null,
          "provider": null, "providerPlaceId": null, "roadAddress": "...", "jibunAddress": null },
        { "visitOrder": 2, "name": "연남 감성 카페", "source": "PERSONAL", "placeId": 41, "isAnchor": true, "...": "" },
        { "visitOrder": 3, "name": "연남 포토부스", "source": "EXTERNAL", "placeId": null, "isAnchor": false,
          "provider": "KAKAO", "providerPlaceId": "987654321", "...": "" },
        { "visitOrder": 4, "name": "연남 와인바", "source": "SHARED", "placeId": 81, "isAnchor": false, "...": "" } ],
      "walkingLegs": [ { "fromVisitOrder": 1, "toVisitOrder": 2, "estimatedMinutes": 5 }, { "...": "" } ],
      "totalWalkingMinutes": 20 } ],
  "reason": null,
  "walkingTimeMethod": "COORDINATE_ESTIMATE"
}
```
- 보통 2~3개, 유효한 코스가 1개면 1개를 돌려준다. 결과가 없으면 `courses: []`와 `reason`을 돌려준다.
  - `INSUFFICIENT_PLACES`: 조건에 맞게 이을 장소 부족
  - `UNSUPPORTED_REGION`: 지원 지역 밖
  - `NO_MORE_COURSES`: (확장) 다시 추천에서 더 없음
- `placeId`·`source`(확장): 저장 장소면 요청에서 받은 `placeId`를 그대로 돌려준다. 외부 후보는 `placeId`가 null이고 `source`는 `EXTERNAL`이다.
- `provider`·`providerPlaceId`·`roadAddress`·`jibunAddress`: 외부 후보에 채워진다(PlaceCandidate와 같은 뜻). 외부 후보를 코스로 저장하려면 이 값으로 먼저 보관함에 저장한다. 저장 장소는 Spring이 가진 값을 쓰므로 `provider`·`providerPlaceId`가 null이고, `roadAddress`에는 요청에서 받은 `address`가 들어간다.
- 잘못된 형식은 400, 필수 장소 정보 없음은 404, 외부 검색 실패는 503이다. 외부 장애를 유효한 추천처럼 돌려주지 않는다.

**추천 규칙** (`placeflow/v3/recommend.py`)

| 영역 | 규칙 |
|---|---|
| 자리 | 각 방문 위치에는 `categorySequence`의 그 카테고리 장소만 들어간다 |
| 필수 장소 | `requiredPlaceOrder` 위치에 고정한다. **카테고리가 그 자리와 달라도 그대로 넣고 경고하지 않는다** |
| 범위 | 기준(필수 장소 또는 지역 중심) 반경 1,000m. 지역은 자동으로 넓히지 않는다 |
| 이동 | 이웃 장소 사이 추정 도보 10분 이내. 도보 분 = 직선거리 × 1.3 ÷ 4.5km/h, 올림(좌표 기반 추정, 실제 경로 아님) |
| 후보 | 저장 장소(가산점) + 외부 후보(카테고리별 가까운 순 최대 12곳). 저장 장소와 이름이 같고 80m 이내인 외부 후보는 중복으로 본다 |
| 선택 | 점수 높은 순으로 서로 다른 코스 최대 3개(필수 장소 외 겹침 1곳 → 2곳 순으로 완화) |
| 제목 | `{지역} {카테고리 이름들} 코스` (예: 연남 맛집·카페·놀거리·술집 코스) |

### 3.6 (확장) 교체 후보 — `POST /internal/v1/place-candidates/replacements`
```json
{ "coursePlaces": [ { "name": "...", "latitude": 37.56, "longitude": 126.92, "categoryCode": "MEAL", "placeId": 12, "source": "PERSONAL" } ],
  "replaceVisitOrder": 2, "categoryCode": null, "savedPlaces": [], "size": 8 }
```
- 응답은 `{ "items": [ PlaceCandidate + placeSource, placeId, walkFromPrevMinutes, walkToNextMinutes ] }`이다.
- 앞·뒤 장소 **양쪽에서** 추정 도보 10분 이내이고 코스에 없는 장소만 고르고, 저장 장소가 먼저 나온다.
- `categoryCode`가 없으면 교체할 장소와 같은 카테고리로 찾는다.
- 주변 놀거리처럼 단순한 주변 검색은 P02(좌표)로 한다.

### 3.7 `GET /health` (인증 없음)
`{ "status": "UP", "workflowVersion": "2.1", "searchProvider": "kakao_local", "extractor": "heuristic", "ocr": "auto", "ocrReady": true, "authRequired": true, "tagProvider": "naver_blog" }`

---

## 4. 외부 API 역할

| 용도 | API | 키 | 비고 |
|---|---|---|---|
| 장소 검색 (P02·P03·P04·R01) | 카카오 로컬 | `KAKAO_REST_API_KEY` (카카오 디벨로퍼스 앱 → 카카오맵 사용 설정 ON) | 좌표·반경·카테고리 그룹 검색. 무료 쿼터는 계정의 첫 앱에만 |
| 장소 태그 (P03·P04) | 네이버 블로그 검색 | `NAVER_CLIENT_ID`·`NAVER_CLIENT_SECRET` (NAVER API HUB, 검색 > 블로그 추가) | 하루 25,000회. 키가 없으면 태그 없이 동작 |
| 태그 판단 (P03·P04) | OpenAI | `OPENAI_API_KEY`, 모델 `TAG_LLM_MODEL`(기본 `gpt-4o-mini`) | **유료**(장소 1곳당 1회). 키가 없으면 태그 없이 동작 |
| 캡쳐 재인식 (P04) | OpenAI · Anthropic · Gemini 중 선택 | `AI_VISION_FALLBACK`, `AI_VISION_FALLBACK_MODEL` + 제공자 키 | **유료**(OCR로 추출하지 못한 요청마다). 기본 꺼짐. 캡쳐 원본이 외부 LLM으로 전송된다 |

- `SEARCH_PROVIDER`: `kakao` · `demo`(가상 데이터) · `demo_fail`(외부 실패 흉내). 비우면 카카오 키가 있으면 kakao, 없으면 demo. 실행 중 자동 전환 없음.
- v2.1부터 네이버 지역검색은 장소 검색에 쓰지 않는다(`SEARCH_PROVIDER=naver`는 시작 오류).
- `place_id`(내부)는 이름 + 도로명주소 해시라서, 같은 가게면 같은 값이다. 응답에는 내보내지 않고, 대신 카카오 장소 ID를 `providerPlaceId`로 내보낸다.

---

## 5. 환경 변수

| 이름 | 용도 | 기본 |
|---|---|---|
| `SEARCH_PROVIDER` | `kakao` · `demo` · `demo_fail` | 카카오 키 있으면 kakao |
| `KAKAO_REST_API_KEY` | 카카오 로컬 | — |
| `NAVER_CLIENT_ID` · `NAVER_CLIENT_SECRET` · `NAVER_SEARCH_ENDPOINT` | 네이버 블로그 검색 → 장소 태그 (`hub` 기본 / `legacy`) | — |
| `OPENAI_API_KEY` · `TAG_LLM_MODEL` | 태그 판단 LLM | gpt-4o-mini |
| `BLOG_TAGS` | `off`면 키가 있어도 태그 끄기 | 켬 |
| `AI_SERVICE_KEY` | 설정하면 `X-AI-Key` 필수 | 끔 |
| `AI_LLM` · `AI_LLM_MODEL` | 추출기 `heuristic` · `openai` · `anthropic` · `gemini` | heuristic |
| `AI_OCR` · `AI_OCR_CONCURRENCY` · `TESSDATA_PREFIX` | OCR 설정 | auto · 2 |
| `AI_VISION_FALLBACK` · `AI_VISION_FALLBACK_MODEL` | OCR + 규칙 기반으로 추출하지 못한 캡쳐만 다시 읽을 비전 LLM (`openai` · `anthropic` · `gemini`) | none |
| `AI_MAX_IMAGE_MB` | 이미지 장당 최대 크기 | 5 |
| `AI_CORS_ORIGINS` | 허용 출처(쉼표 구분). 백엔드끼리만 호출하므로 기본은 끔 | — |

`.env`는 커밋하지 않는다.

---

## 6. Python 모듈 (AI 로직)

```python
from placeflow.v3.search import get_search_provider          # kakao | demo | demo_fail
search = get_search_provider()
search.search("잠실 카페", 5)                                   # 키워드 → list[Place]
search.nearby(37.51, 127.10, 1000, code="CAFE", query=None)    # 좌표 주변 → 가까운 순

from placeflow.v3.recommend import RecommendRequest, recommend  # R01 엔진 (외부 실패: ExternalSearchError)
from placeflow.v3.save_flow import candidates_from_records, match  # 인식 후보 매칭
from placeflow.v3.category_infer import infer_category          # 카테고리 없는 저장 장소 추론
from placeflow.v3.categories import CODES, LABELS, SEARCH_PLAN  # 카테고리 7개와 검색 계획
from placeflow.v3.blog_tags import get_tagger, LLMTagJudge     # 블로그 제목·요약 → OpenAI 태그 판단
```

---

## 7. 테스트

```bash
python3 -m pip install -r requirements-api.txt
python3 -m pytest -q tests/test_v3.py tests/test_ai_api.py tests/test_blog_tags.py   # 엔진·매칭·카카오/네이버 블로그(가짜 HTTP)·내부 API 계약
python3 -m placeflow api --port 8001                         # → /docs 에서 직접 호출
```

---

## 8. 미정·주의

- 2026-11-04 실제 키 확인: 카카오 키워드 검색과 OpenAI(`gpt-4o-mini`) 비전 추출은 호출에 성공했고, 네이버 블로그 태그는 `tag_source: naver_blog` 응답을 확인했다. 내부 API P04 경로의 실제 호출과 Anthropic·Gemini는 확인하지 않았다. 자동 테스트는 가짜 HTTP 응답을 쓴다.
- `/health`의 `extractor`는 `AI_LLM` 값만 보여 준다. 비전 LLM 폴백이 켜져 있는지는 표시되지 않는다.
- 태그 품질은 LLM 판단과 블로그 요약 길이에 좌우된다. 요약이 짧아서 글이 적은 가게는 태그가 비거나 적다.
- 지원 지역 6곳의 중심 좌표·반경은 근사값이다.
- 카테고리 추론(방법 b)은 검색 품질에 좌우된다. DB에 `categoryCode`를 두면(방법 a) 더 정확하고 외부 호출도 줄어든다.
- 외부 후보 저장·캐시 범위는 제공자 약관 확인 후 정한다. 지금은 메모리 캐시 6시간만 쓴다.
- (확장) 표시가 붙은 필드·엔드포인트는 TRD 초안에 없는 제안이다. 서버 설계 담당과 합의 후 TRD에 반영하거나 뺀다.
- 실험 서버(`python -m placeflow serve`)의 `/api/v3/*`는 실험 화면 전용이며 내부 API 계약이 아니다. 관련 파일은 `.gitignore`에 들어 있는 로컬 전용이다.
