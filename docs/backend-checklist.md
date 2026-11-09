# 백엔드 확인 요청 (AI → 서버)

> AI 서버를 띄웠습니다. 연동 전에 아래 항목을 확인해 주세요. · 2026-11-09
> 연동 상세(DTO·필드 매핑·오류 처리)는 [BACKEND_INTEGRATION.md](BACKEND_INTEGRATION.md)를 봐 주세요.

---

## 0. 한 줄 요약

- AI 서버 주소는 `https://hansol.tail9cc9e7.ts.net:8443`입니다. 서비스 키는 따로 보내 드립니다.
- 모든 `/internal/v1/*` 요청에 `X-AI-Key` 헤더가 필요합니다. 키가 없으면 401입니다.
- 11/06에 응답 필드 4개(`provider`·`providerPlaceId`·`roadAddress`·`jibunAddress`)를 추가했습니다. 기존 필드는 그대로입니다.
- 아직 정하지 않은 항목이 6개 있습니다(5절).

---

## 1. 접속 정보

| 항목 | 값 |
|---|---|
| Base URL (`AI_SERVER_BASE_URL`) | `https://hansol.tail9cc9e7.ts.net:8443` |
| 서비스 키 (`AI_SERVICE_KEY`) | 개인 메시지로 따로 전달 |
| 인증 헤더 | `X-AI-Key: {AI_SERVICE_KEY}` — 모든 `/internal/v1/*` 요청에 필수 |
| API 문서 (Swagger) | `https://hansol.tail9cc9e7.ts.net:8443/docs` |
| 상태 확인 | `GET /health` (키 없이 호출 가능) |

```yaml
ai:
  server:
    base-url: https://hansol.tail9cc9e7.ts.net:8443   # 경로 /internal/v1 은 호출 쪽에서 붙임
    service-key: ${AI_SERVICE_KEY}
```

**확인해 주세요**
- [ ] Spring이 실행되는 곳에서 **8443 포트로 나가는 HTTPS 요청**이 막혀 있지 않은지 (학교·회사 네트워크는 443 외 포트를 막는 경우가 있음)
- [ ] 서비스 키를 코드나 저장소에 넣지 말고 환경 변수로만 넣기
- [ ] `BACKEND_INTEGRATION.md`의 `http://127.0.0.1:8001`은 로컬 기준입니다. 공유 서버는 위 주소를 써 주세요.

**알아 둘 점**
- 개발용 서버라 AI 파트 PC에서 돌아갑니다. PC가 꺼지면 접속이 안 되니, 시연 전에 `/health`로 꼭 확인해 주세요.
- 서비스 키가 바뀌면 다시 알려 드리겠습니다. 키가 바뀌면 이전 키로 보낸 요청은 401이 됩니다.

---

## 2. 연결 확인

아래 세 가지가 모두 기대한 대로 나오면 연결된 것입니다.

```bash
# ① 상태 — 키 없이 200
curl -s https://hansol.tail9cc9e7.ts.net:8443/health

# ② 키 없이 → 401 (AUTH-0401, INVALID_SERVICE_KEY)
curl -s -o /dev/null -w '%{http_code}\n' https://hansol.tail9cc9e7.ts.net:8443/internal/v1/regions

# ③ 키 넣고 → 200
curl -s https://hansol.tail9cc9e7.ts.net:8443/internal/v1/regions -H "X-AI-Key: $AI_SERVICE_KEY"
```

①의 현재 응답 (2026-11-09 기준):
```json
{"status":"UP","workflowVersion":"2.1","searchProvider":"kakao_local","extractor":"heuristic",
 "ocr":"auto","ocrReady":true,"authRequired":true,"tagProvider":"naver_blog+openai:gpt-4o-mini"}
```
- `searchProvider`가 `kakao_local`이면 실제 카카오 데이터입니다. `demo`면 가상 데이터이니 AI 파트에 알려 주세요.
- `authRequired: true`면 키 검사가 켜진 상태입니다.

장소 인식이 실제로 되는지 볼 때는 아래 문구를 써 주세요.
```bash
curl -s -X POST https://hansol.tail9cc9e7.ts.net:8443/internal/v1/place-recognitions \
  -H 'Content-Type: application/json' -H "X-AI-Key: $AI_SERVICE_KEY" \
  -d '{"sharedText":"오지다 방이동 맛집"}'
# → "status":"CANDIDATES_FOUND", candidates[0].providerPlaceId 에 카카오 장소 id
```
> `BACKEND_INTEGRATION.md` 7절의 예시(`1. 오지다 (서울 송파구 백제고분로48길 24)`)는 실제 카카오 서버에서 `NEEDS_SEARCH`가 나옵니다. 오류가 아니라 "못 찾음" 응답(200)입니다.

---

## 3. 이번에 바뀐 응답 필드 (2026-11-06)

`PlaceCandidate`(P02·P03·P04·교체 후보)와 코스 추천의 `CoursePlaceOut`(R01)에 필드 4개를 추가했습니다.

| 필드 | 값 | null인 경우 |
|---|---|---|
| `provider` | `"KAKAO"` (로컬 데모 데이터는 `"DEMO"`) | 링크에서만 읽고 검색으로 확인하지 못한 장소, 코스 안의 저장 장소 |
| `providerPlaceId` | 카카오 장소 `id` (문자열) — 보관함 저장 키 | 위와 같음 |
| `roadAddress` | 도로명 주소 | 없을 때 |
| `jibunAddress` | 지번 주소 | 없을 때 |

- 기존 `address`는 표시용 한 줄(도로명, 없으면 지번)로 그대로 남아 있습니다.
- 코스 안의 저장 장소(`source: PERSONAL`·`SHARED`)는 `provider`·`providerPlaceId`가 null이고 `roadAddress`에 요청에서 보낸 `address`가 들어갑니다. 저장 장소는 `placeId`로 Spring DB 값을 쓰면 됩니다.

**확인해 주세요**
- [ ] 응답 DTO가 **모르는 필드를 무시**하는지 (Jackson `FAIL_ON_UNKNOWN_PROPERTIES = false` 또는 `@JsonIgnoreProperties(ignoreUnknown = true)`). 앞으로도 필드를 추가할 수 있습니다.
- [ ] `providerPlaceId`가 null인 후보는 보관함에 저장하지 않고 직접 검색으로 안내하기
- [ ] 보관함 저장 매핑: `address` ← `jibunAddress`(없으면 `roadAddress`), `roadAddress` ← `roadAddress`, `lat`/`lng` ← `latitude`/`longitude` (자세한 표는 BACKEND_INTEGRATION.md 4.4절)
- [ ] 추천 코스의 외부 장소(`source: EXTERNAL`)는 **보관함에 먼저 저장한 뒤** 코스를 저장하기

---

## 4. 설정 확인

| 항목 | AI 파트 권장 | 확인 |
|---|---|---|
| read-timeout | 스크린샷(P04) **70초** · 링크·텍스트(P03) 30초 · 코스 추천(R01) 15초 · 나머지 10초 | [ ] |
| 재시도 | 조회(P01·P02)만 1회. **P03·P04·R01은 재시도하지 않기** (OpenAI 호출이 다시 나가서 비용·시간이 두 배) | [ ] |
| `sharedText` 길이 | AI는 20,000자까지 받음. Spring 제한(현재 2,000자)을 늘리는 것 권장 (인스타 캡션이 길 때) | [ ] |
| 이미지 | JPEG·PNG·WebP, 장당 5MiB 이하, 추가 캡쳐(`extraImages`) 최대 4장 | [ ] |
| 오류 응답 | `application/problem+json`. `status`·`code`·`reason`으로 분기 (BACKEND_INTEGRATION.md 6절) | [ ] |
| AI 서버 장애 시 | 연결 실패·503이면 인식은 직접 검색(`NEEDS_SEARCH`)으로 폴백 | [ ] |

**비전 폴백이 켜져 있습니다** (`AI_VISION_FALLBACK=openai`, `gpt-4o-mini`)
- OCR로 장소를 못 찾은 캡쳐만 OpenAI로 다시 읽습니다. 이때 **캡쳐 원본이 OpenAI로 전송**됩니다.
- 시연용으로 켜 둔 것입니다. 운영에서도 켤지는 개인정보·비용을 보고 함께 정해야 합니다(5절 ⑨).

---

## 5. 아직 정하지 않은 항목

BACKEND_INTEGRATION.md 8절에서 ①·②는 반영을 마쳤고, 나머지는 백엔드 답변이 필요합니다.

| # | 내용 | AI 파트 제안 | 답변 |
|---|---|---|---|
| ③ | 보관함에 `categoryCode`(7개 코드) 저장 여부 | 저장 권장 — 추천 정확도가 오르고 카카오 호출이 줄어듦 | |
| ④ | 지역 ID 표기 | 한쪽으로 통일 (아래 표) | |
| ⑤ | 코스 추천 엔진: Spring 유지(A) vs AI R01 위임(B) | B. 앱 요청에 `placeCount`·`categorySequence`·`requiredPlaceOrder` 추가 필요 | |
| ⑥ | 블로그 태그(`tags`) 보관함 저장 여부·숨김 기준 | `tagPostCount < 3`이면 숨김 | |
| ⑦ | P03·P04 재시도 제거, read-timeout 조정 | 4절대로 | |
| ⑧ | `sharedText` 길이 제한 2,000 → 늘리기 | 20,000자까지 가능 | |
| ⑨ | 비전 폴백을 운영에서 켤지 | 시연은 켬. 운영은 개인정보·비용 확인 후 결정 | |

**④ 지역 ID — 현재 AI 서버 실제 값** (`GET /internal/v1/regions`, 2026-11-09 확인)

| Spring `SupportedArea` | AI `regionId` |
|---|---|
| `JAMSIL` | `JAMSIL` |
| `SEONGSIN` | **`SUNGSHIN`** ⚠️ |
| `HANSEONG` | **`HANSUNG`** ⚠️ |
| `HYEHWA` | `HYEHWA` |
| `SEOCHON` | `SEOCHON` |
| `YEONNAM` | `YEONNAM` |

통일하기 전까지는 Spring에서 두 곳을 변환해서 보내야 합니다. 안 바꾸고 보내면 **오류 없이 200 + `courses: []` + `reason: "UNSUPPORTED_REGION"`**이 와서, "추천할 코스가 없음"처럼 보입니다(2026-11-09 확인: `SEONGSIN` → 빈 결과, `SUNGSHIN` → 코스 3개).

---

## 6. 문의

- 연결이 안 되거나 예상과 다른 응답이 오면 **요청 경로, 응답 status, 응답 본문의 `code`·`reason`**을 같이 알려 주세요.
- 필드 추가·이름 변경이 필요하면 AI 파트에 알려 주세요.
