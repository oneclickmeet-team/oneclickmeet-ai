# 백엔드(Spring Boot) ↔ AI 서버(FastAPI) 연동 가이드

> 대상: 백엔드 작업자 · 작성: AI 파트 · 기준: AI 워크플로우 v2.1 (2026-11-06)
> 상세 명세: [API_CONVENTION.md](API_CONVENTION.md) · OpenAPI: [openapi.json](openapi.json) · 실행 후 Swagger `http://127.0.0.1:8001/docs`
>
> 이 문서는 **지금 `oneclickmeet-server` 코드 기준으로 무엇을 바꿔야 붙는지**를 정리한 것입니다.
> 필드 하나하나의 규칙은 API_CONVENTION.md에 있습니다.

---

## 0. 한 줄 요약

- 서버 코드는 AI 서버를 `POST /v1/places/extract/text|image` → `{placeNames: [...]}` 로 부르도록 되어 있습니다(제안안).
- 실제 AI 서버는 **`/internal/v1/*`** 경로이고, 이름만 주지 않고 **카카오 검색·매칭이 끝난 후보 목록**을 돌려줍니다.
- 그래서 연동하려면 (1) 호출 경로·요청·응답 DTO를 바꾸고, (2) 후보 → 보관함 저장 필드 매핑을 맞추고, (3) 코스 추천을 Spring 엔진으로 둘지 AI R01로 넘길지 정해야 합니다.
- **AI 쪽 반영 완료 (2026-11-06):** 후보와 추천 코스의 외부 장소에 `provider`·`providerPlaceId`(카카오 장소 id)와 `roadAddress`·`jibunAddress`를 추가했습니다. 보관함 저장에 필요한 값을 그대로 받을 수 있습니다.

---

## 1. AI 서버 띄우기

```bash
cd oneclickmeet-ai
python3 -m pip install -r requirements.txt -r requirements-api.txt
cp .env.example .env          # 키 채우기 (아래 표)
python3 -m placeflow api --port 8001
# 배포: uvicorn placeflow.ai_api.main:app --host 0.0.0.0 --port 8001
```

- 스크린샷 인식(P04)을 쓰려면 서버에 Tesseract 한국어 데이터가 있어야 합니다: `brew install tesseract tesseract-lang` (Ubuntu: `apt install tesseract-ocr tesseract-ocr-kor`).
- 확인: `curl http://127.0.0.1:8001/health` → `"status":"UP"`, `"ocrReady":true`, `"searchProvider":"kakao_local"`

### AI 서버 환경 변수 (AI 서버 쪽 `.env`)

| 이름 | 필수 | 설명 |
|---|---|---|
| `KAKAO_REST_API_KEY` | O | 장소 검색. 없으면 가상 데모 데이터로 동작 |
| `AI_SERVICE_KEY` | 운영 O | 설정하면 모든 `/internal/v1/*` 요청에 `X-AI-Key` 헤더 필수 (없거나 틀리면 401) |
| `NAVER_CLIENT_ID` · `NAVER_CLIENT_SECRET` · `OPENAI_API_KEY` | | 장소 태그. 없으면 태그 없이 동작 |
| `AI_VISION_FALLBACK` · `AI_VISION_FALLBACK_MODEL` | | OCR로 못 읽은 캡쳐를 비전 LLM으로 재시도 (기본 끔, 유료) |
| `AI_MAX_IMAGE_MB` | | 이미지 장당 최대 크기, 기본 5 |

---

## 2. Spring 쪽 설정

현재 `AiServerProperties`(`ai.server.*`)를 그대로 쓰면 됩니다. 추가가 필요한 것:

```yaml
ai:
  server:
    base-url: ${AI_SERVER_BASE_URL:}      # 예: http://localhost:8001  (경로 /internal/v1 은 호출 쪽에서 붙임)
    service-key: ${AI_SERVICE_KEY:}       # 신규 — AI 서버와 같은 값. 요청 헤더 X-AI-Key 로 보냄
    connect-timeout: 2s
    read-timeout: 30s                     # 현재 기본 10s → 인식(P03·P04)에는 부족
```

| 항목 | 권장 |
|---|---|
| 인증 헤더 | 모든 `/internal/v1/*` 요청에 `X-AI-Key: {AI_SERVICE_KEY}` (`/health`는 불필요) |
| 타임아웃 | P03·P04 **30초**(비전 LLM 폴백을 켜면 최대 약 60초), R01 15초, 나머지 10초 |
| 재시도 | P01·P02(조회)만 1회. **P03·P04·R01은 재시도하지 않기** — 지금 `FastApiPlaceNameExtractor`는 5xx·연결 실패 시 재시도하는데, 인식은 OpenAI 호출이 들어가 비용·시간이 두 배가 됩니다 |
| 성공 응답 | 래퍼 없이 DTO 그대로, JSON **camelCase** |
| 오류 응답 | `application/problem+json` — `status`·`code`·`reason`으로 분기 (아래 6절) |

---

## 3. 엔드포인트 목록

| 용도 | AI 내부 API | 지금 Spring | 연동 필요 |
|---|---|---|---|
| 링크·공유 텍스트로 장소 찾기 | `POST /internal/v1/place-recognitions` | `POST /api/v1/places/resolve/link` → AI `/v1/places/extract/text` | **O (경로·DTO 변경)** |
| 스크린샷으로 장소 찾기 | `POST /internal/v1/place-image-recognitions` (multipart) | `POST /api/v1/places/resolve/image` → AI `/v1/places/extract/image` | **O (경로·DTO 변경)** |
| 코스 추천 | `POST /internal/v1/course-recommendations` | Spring 자체 엔진 (`CourseRecommendationService`) | 결정 필요 (5절) |
| 지원 지역 | `GET /internal/v1/regions` | `SupportedArea` enum | 선택 |
| 키워드·좌표 장소 검색 | `GET /internal/v1/place-candidates` | Spring이 카카오 직접 호출 | 선택 (지금대로 둬도 됨) |
| 코스 편집 교체 후보 | `POST /internal/v1/place-candidates/replacements` | 없음 | 선택 |
| 카테고리 코드 7개 | `GET /internal/v1/categories` | 없음 | 선택 |
| 상태 확인 | `GET /health` | — | 운영 모니터링용 |

---

## 4. 장소 인식 연동 (P03·P04) — 필수

### 4.1 링크·텍스트 — `POST /internal/v1/place-recognitions`

요청 (지금 `{"text": ...}` → 바꿔야 함):
```json
{
  "sourceUrl": null,
  "sharedText": "앱에서 공유받은 문구 그대로 (링크 포함 가능)",
  "savedPlaces": [ { "placeId": 7, "name": "닭튜브 방이점", "latitude": 37.5131, "longitude": 127.1123 } ]
}
```
- `sourceUrl`·`sharedText` 중 하나 이상 필수. 지금 Spring은 `sharedText`만 받으니 그대로 넣으면 됩니다(AI가 첫 링크를 꺼내 씁니다).
- `sharedText` 최대 20,000자 (Spring은 현재 2,000자로 제한 — 인스타 캡션이 길면 늘리는 것 권장).
- `savedPlaces`는 선택. 보내면 이미 보관함에 있는 장소 후보에 `savedPlaceId`가 채워져 "이미 저장됨" 표시를 할 수 있습니다.

### 4.2 스크린샷 — `POST /internal/v1/place-image-recognitions`

`multipart/form-data`
| 파트 | 필수 | 설명 |
|---|---|---|
| `image` | O | JPEG·PNG·WebP, 5MiB 이하 (지금 Spring 검증 로직과 같음) |
| `extraImages` | | 같은 장소의 추가 캡쳐 최대 4장 |
| `sharedText` | | 함께 붙여넣은 캡션 |

지금 `fromImage`의 multipart 구성(`image` 파트 + content-type)은 그대로 쓰고 URI만 바꾸면 됩니다.

### 4.3 응답 (P03·P04 공통 `RecognitionResponse`)

```json
{
  "status": "CANDIDATES_FOUND",
  "extractedQuery": "오지다",
  "candidates": [
    { "name": "오지다", "address": "서울특별시 송파구 백제고분로48길 24",
      "latitude": 37.5131, "longitude": 127.1123,
      "sourceUrl": "http://place.map.kakao.com/123456789",
      "provider": "KAKAO", "providerPlaceId": "123456789",
      "roadAddress": "서울특별시 송파구 백제고분로48길 24", "jibunAddress": "서울 송파구 방이동 44-1",
      "categoryLabel": "맛집", "categoryCode": "MEAL",
      "openingHours": null, "distanceMeters": null, "savedPlaceId": null,
      "matchType": "EXACT", "matchReasons": ["주소 일치", "이름 일치"],
      "tags": [ { "label": "음식이 맛있어요", "mentionCount": 12 } ], "tagPostCount": 31 }
  ],
  "sourceUrl": null,
  "message": null,
  "groups": [ { "order": 1, "extractedName": "오지다", "extractedAddress": "...", "status": "MATCHED",
                "best": { "...": "후보" }, "others": [ { "...": "후보" } ] } ],
  "tagSource": "NAVER_BLOG"
}
```

Spring이 할 일:
- **AI가 이미 카카오 검색까지 해서 후보를 줍니다.** 지금 `PlaceResolveService.candidatesFor()`처럼 이름으로 카카오를 다시 검색할 필요가 없습니다.
- `status = NEEDS_SEARCH`면 앱에 직접 검색 화면으로 보내고, 검색창에 `extractedQuery`를 넣어 주세요. 오류가 아니라 200입니다.
- `matchType`: `EXACT`만 기본 선택, `SIMILAR`는 접어서 `matchReasons`와 함께 보여 주는 것을 권장합니다.
- `groups`: 릴스 하나에 가게가 여러 곳 나올 때(예: '맛집 4곳') 장소별로 묶은 결과입니다. 여러 곳 저장 화면을 만들 거면 `groups`를, 아니면 `candidates`만 써도 됩니다.
- `tags`: 블로그 언급 기반 태그(최대 5개). 저장할 때 같이 보관할지는 백엔드가 정합니다. `tagPostCount`가 적으면(예: 3 미만) 숨기는 것을 권장합니다.
- AI 서버를 못 쓰면(연결 실패·503) 지금처럼 `SharedTextNameHeuristic` 또는 `NEEDS_SEARCH`로 내려가는 폴백은 그대로 유지하면 됩니다.

### 4.4 후보 → 보관함 저장(`SaveSavedPlaceRequest`) 필드 매핑

| Spring `SaveSavedPlaceRequest` | AI `PlaceCandidate` | 비고 |
|---|---|---|
| (`provider` enum `KAKAO`) | `provider` | `"KAKAO"`. 로컬에서 카카오 키 없이 띄우면 `"DEMO"`(가상 데이터 — 테스트용) |
| `providerPlaceId` (필수) | `providerPlaceId` | 카카오 장소 `id` 그대로 (Spring 카카오 클라이언트가 쓰던 값과 같음) |
| `name` | `name` | |
| `address` (필수) | `jibunAddress` (없으면 `roadAddress`) | Spring 카카오 클라이언트는 `address`=지번(`address_name`), `roadAddress`=도로명으로 저장 중 → 같은 기준 |
| `roadAddress` | `roadAddress` | 없으면 null |
| `category` | `categoryLabel` | AI는 카카오 원본 분류 문자열이 아니라 우리 7개 라벨(맛집·카페·술집·놀거리·전시·문화·산책·쇼핑) |
| `categoryGroupCode` | (없음) | 대신 `categoryCode`(MEAL·CAFE·BAR·ACTIVITY·CULTURE·WALK·SHOPPING) 제공. 저장 컬럼 추가 권장 ③
| `lat` / `lng` | `latitude` / `longitude` | 이름만 다름 |
| `placeUrl` | `sourceUrl` | |

- 후보는 카카오 검색 결과에서 나오므로 보통 `providerPlaceId`가 채워져 있습니다. 혹시 null이면(검색으로 확인하지 못한 장소) 저장하지 말고 직접 검색으로 안내하세요.

---

## 5. 코스 추천 (R01) — 결정 필요

지금 Spring에는 자체 추천 엔진이 있고(`CourseRecommendationService` + `CourseBuilder`, 키워드 "카페·맛집·전시" 검색), AI 서버에도 R01 엔진이 있습니다. 둘 중 하나를 정해야 합니다.

| | A. Spring 엔진 유지 | B. AI R01로 위임 (AI 파트 권장) |
|---|---|---|
| 기능 | 기준 장소/지역 + 반경 1km + 도보 10분 | 위 + **장소 수 3~5 선택 · 방문 순서별 카테고리 지정 · 필수 장소 위치 지정 · 다시 추천받기(제외 키) · 코스 제목 · 7개 카테고리 추론** |
| Spring 작업 | 없음 | 요청 DTO 확장 + AI 호출 + 응답 매핑 |
| 카카오 호출 | Spring | AI 서버 (Spring은 호출 안 함) |

### B를 고를 때 Spring → AI 요청

```json
{
  "anchor": { "type": "PLACE", "placeId": 41 },
  "placeCount": 4,
  "categorySequence": ["MEAL", "CAFE", "ACTIVITY", "BAR"],
  "requiredPlaceOrder": 2,
  "anchorPlace": { "placeId": 41, "source": "PERSONAL", "name": "연남 감성 카페", "address": "...",
                   "latitude": 37.5631, "longitude": 126.9282, "categoryCode": null },
  "savedPlaces": [ { "placeId": 12, "source": "PERSONAL", "name": "연남 파스타집",
                     "latitude": 37.5626, "longitude": 126.9255, "categoryCode": null } ],
  "excludeCandidateKeys": []
}
```

| AI 필드 | Spring에서 채우는 값 |
|---|---|
| `anchor` | 기준 장소면 `{type:"PLACE", placeId: anchorSavedPlaceId}`, 지역이면 `{type:"REGION", regionId}` |
| `placeCount` · `categorySequence` · `requiredPlaceOrder` | **앱 요청에 새로 받아야 함** (지금 `RecommendCoursesRequest`에는 없음). REGION이면 `requiredPlaceOrder`를 보내지 않음(보내면 400) |
| `anchorPlace` / `savedPlaces[]` | `SavedPlace` → `placeId=id`, `source="PERSONAL"`, `name`, `address=roadAddress ?: address`, `latitude=lat`, `longitude=lng` (최대 500곳) |
| `savedPlaces[].categoryCode` | 없으면 `null` → AI가 추론. 있으면 넣기: 카카오 `CE7→CAFE`, `CT1→CULTURE`, `AT4→WALK`, `FD6→MEAL`(술집은 AI 추론에 맡기려면 `null`) |
| 공동 장소 | 커플 공동 장소가 생기면 `source:"SHARED"`로 함께 넣기. 상대의 비공개 보관함은 넣지 않기 |

**지역 ID가 서로 다릅니다** ⚠️

| Spring `SupportedArea` | AI `regionId` |
|---|---|
| `JAMSIL` | `JAMSIL` |
| `SEONGSIN` | **`SUNGSHIN`** |
| `HANSEONG` | **`HANSUNG`** |
| `HYEHWA` | `HYEHWA` |
| `SEOCHON` | `SEOCHON` |
| `YEONNAM` | `YEONNAM` |

→ 매핑하거나 한쪽 이름으로 통일 (결정 필요 ④)

### AI 응답 → Spring `CourseRecommendationResponse` 매핑

| Spring | AI | 비고 |
|---|---|---|
| `status` | `courses`가 비었으면 `NOT_ENOUGH_CANDIDATES` | AI `reason`: `INSUFFICIENT_PLACES`·`UNSUPPORTED_REGION`·`NO_MORE_COURSES` |
| `courses[].candidateKey` | `candidateKey` | 이번 응답에서만 쓰는 값, 영구 ID 아님 |
| (없음) | `title` | 예: "연남 맛집·카페·놀거리·술집 코스" — 추가 권장 |
| `totalWalkMinutes` | `totalWalkingMinutes` | |
| `places[].order` | `visitOrder` | |
| `places[].savedPlaceId` | `placeId` (`source`가 PERSONAL·SHARED일 때) | 외부 후보는 `placeId: null`, `source: "EXTERNAL"` |
| `places[].walkMinutesFromPrevious` | `walkingLegs[toVisitOrder = n].estimatedMinutes` | 첫 장소는 null |
| `providerPlaceId` · `placeUrl` | `providerPlaceId` · `sourceUrl` | 외부 장소만 채워짐. 저장 장소는 `placeId`로 Spring DB 값을 쓰면 됨 |
| `address` · `roadAddress` | `jibunAddress` · `roadAddress` | 4.4와 같음 |
| `lat` · `lng` · `category` | `latitude` · `longitude` · `categoryLabel` (+ `categoryCode`) | |

**코스 저장 주의:** `CreateCourseRequest.places[].placeId`는 보관함 장소 ID라서, 추천 코스에 들어간 외부 장소(`source: EXTERNAL`)는 **먼저 보관함에 저장한 뒤** 코스를 저장해야 합니다. 저장에 필요한 `providerPlaceId`·`roadAddress`·`jibunAddress`가 코스 장소에 같이 들어 있습니다.

---

## 6. 오류 처리

AI 서버 오류는 모두 `application/problem+json`입니다.
```json
{ "type": "about:blank", "title": "Service Unavailable", "status": 503, "detail": "...",
  "instance": "/internal/v1/course-recommendations", "code": "EXTERNAL-0503", "reason": "PROVIDER_UNAVAILABLE" }
```

| AI status / reason | 원인 | Spring 처리 권장 |
|---|---|---|
| 400 `INVALID_INPUT` (+`errors[]`) | Spring이 잘못 보냄 | 서버 버그로 로그, 앱엔 400 또는 500 |
| 401 `INVALID_SERVICE_KEY` | `X-AI-Key` 불일치 | 설정 오류. 인식은 직접 검색 폴백 |
| 404 `RESOURCE_NOT_FOUND` | R01에서 필수 장소 정보가 요청에 없음 | Spring 버그 (anchorPlace/savedPlaces 누락) |
| 413 `IMAGE_TOO_LARGE` / 415 `UNSUPPORTED_IMAGE_TYPE` | 이미지 검증 실패 | 앱에 400 (Spring이 먼저 걸러서 보통 안 옴) |
| 503 `PROVIDER_UNAVAILABLE` | 카카오 검색 실패·한도 | 앱에 502/503. 인식은 직접 검색으로 안내 |
| 503 `RECOGNIZER_UNAVAILABLE` | AI 서버에 OCR·비전 LLM 없음 | 인식은 `NEEDS_SEARCH` 폴백 |
| 500 `INTERNAL_ERROR` | AI 서버 버그 | 폴백 + AI 파트에 알려 주기 |

- 정상 분기는 오류가 아닙니다: 인식 실패 = `200 + NEEDS_SEARCH`, 추천 결과 없음 = `200 + courses: [] + reason`.
- 네이버·OpenAI 태그 실패는 오류가 아니라 `tags: []`, `tagSource: null`로 옵니다.

---

## 7. 바로 호출해 보기

```bash
# 상태
curl -s http://127.0.0.1:8001/health

# 링크·텍스트 인식
curl -s -X POST http://127.0.0.1:8001/internal/v1/place-recognitions \
  -H 'Content-Type: application/json' -H "X-AI-Key: $AI_SERVICE_KEY" \
  -d '{"sharedText":"1. 오지다 (서울 송파구 백제고분로48길 24)"}'

# 스크린샷 인식
curl -s -X POST http://127.0.0.1:8001/internal/v1/place-image-recognitions \
  -H "X-AI-Key: $AI_SERVICE_KEY" -F 'image=@capture.png;type=image/png'

# 코스 추천 (지역 기준)
curl -s -X POST http://127.0.0.1:8001/internal/v1/course-recommendations \
  -H 'Content-Type: application/json' -H "X-AI-Key: $AI_SERVICE_KEY" \
  -d '{"anchor":{"type":"REGION","regionId":"YEONNAM"},"placeCount":3,"categorySequence":["MEAL","CAFE","WALK"]}'
```
`AI_SERVICE_KEY`를 설정하지 않은 로컬 개발에서는 `X-AI-Key` 헤더를 빼도 됩니다.

---

## 8. 결정 필요 항목 (백엔드와 합의)

| # | 내용 | AI 파트 제안 |
|---|---|---|
| ① | ~~카카오 장소 ID(`providerPlaceId`) 전달 방식~~ | ✅ **반영 완료** — 후보·추천 외부 장소에 `provider`·`providerPlaceId` 추가 |
| ② | ~~주소 한 칸 vs 지번·도로명 두 칸~~ | ✅ **반영 완료** — `roadAddress`·`jibunAddress` 추가 (`address`는 표시용으로 유지) |
| ③ | 보관함에 `categoryCode`(7개 코드) 저장 여부 | 저장 권장 → 추천 정확도↑, AI의 카테고리 추론용 카카오 호출↓ |
| ④ | 지역 ID 표기 (`SEONGSIN`/`SUNGSHIN`, `HANSEONG`/`HANSUNG`) | 한쪽으로 통일. 바꾸는 쪽 합의 |
| ⑤ | 코스 추천 엔진: Spring 유지(A) vs AI R01 위임(B) | B. 앱 요청에 `placeCount`·`categorySequence`·`requiredPlaceOrder` 추가 필요 |
| ⑥ | 블로그 태그(`tags`)를 보관함에 저장할지, 언제 숨길지 | `tagPostCount < 3`이면 숨김 |
| ⑦ | P03·P04 재시도 제거, read-timeout 30초 | 2절대로 |
| ⑧ | `sharedText` 길이 제한 2,000 → 늘리기 | AI는 20,000자까지 받음 |
| ⑨ | 비전 LLM 폴백(유료, 캡쳐 원본이 외부 LLM으로 전송) 운영에서 켤지 | 개인정보·비용 확인 후 결정 |

---

## 9. 알아 둘 점

- AI 서버는 **상태가 없습니다**. DB에 쓰지 않고, 업로드 이미지는 처리 후 바로 지웁니다. 사용자·보관함 정보는 매 요청에 Spring이 담아 보내야 합니다.
- 장소 검색 결과는 AI 서버 메모리에 6시간, 태그는 24시간 캐시됩니다(서버 재시작 시 사라짐).
- 도보 시간은 직선거리 × 1.3 ÷ 4.5km/h로 추정한 값입니다(실제 경로 아님, `walkingTimeMethod: COORDINATE_ESTIMATE`).
- 지원 지역 6곳의 중심 좌표·반경은 근사값입니다.
- (확장) 표시가 붙은 필드·엔드포인트(`groups`·`tags`·교체 후보 등)는 TRD 초안에 없던 제안입니다. 안 쓰면 무시해도 됩니다.
- 새 필드는 추가만 했고 기존 필드는 그대로라서, 이전 형식으로 읽는 코드도 깨지지 않습니다.
- 문의: AI 파트 — 필드 추가·이름 변경이 필요하면 알려 주세요.
