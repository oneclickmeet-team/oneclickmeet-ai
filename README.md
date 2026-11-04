# placeflow — OneClickMeet AI 워크플로우 v2.1

20대 커플이 인스타그램·네이버에서 본 장소를 **링크·스크린샷·직접 검색**으로 저장하고,
저장한 장소를 기준으로 **걸어서 다니는 데이트 코스**를 추천하는 AI 워크플로우입니다.
유료 데이터 API 없이 동작합니다(링크 메타 정보 + 무료 OCR + 카카오 로컬·네이버 블로그 검색 무료 키). OpenAI(유료)는 장소 태그 판단과, 켜 두었을 때 OCR로 못 읽은 캡쳐를 다시 읽는 데만 씁니다. 키나 설정이 없으면 해당 기능 없이 동작합니다.

> **버전 표기:** 이 저장소의 버전(**v1.x · v2.x**)은 **AI 워크플로우 버전**입니다.
> 코드·문서에 나오는 "V3"는 기준으로 삼은 **Figma 와이어프레임 버전**(`와이어프레임(mid-fi) › V3 - 재원` 확정 IA)을 가리킵니다.
> 그래서 코드 폴더 이름이 `placeflow/v3/` 입니다. 워크플로우 버전과는 별개입니다.
> 서비스 전체 설계 기준은 팀 TRD v1.0(2026-10-18)입니다: React Native → **Spring Boot** → FastAPI(AI 내부 서비스).

## v2 워크플로우 한눈에 보기
```
저장   링크(메타 정보)·공유 텍스트·스크린샷(OCR → 실패 시 비전 LLM) → 장소 추출(이름·주소·분류)
       → 장소 검색으로 후보 찾기(장소별 묶음, 확실한 일치/비슷한 이름 구분) → 사용자가 확인 → Spring 이 저장
추천   기준(필수 장소 1곳 + 방문 위치, 또는 지원 지역) + 장소 수 3~5 + 방문 순서별 카테고리
       → 반경 1km 후보(저장 장소 + 외부 후보) → 자리마다 지정 카테고리, 이웃 도보 10분 이내 → 코스 1~3개
```
- 연결: **상태 없는 AI 내부 API** `/internal/v1/*` (camelCase · ProblemDetail). Spring 이 저장 장소를 요청에 담아 보내고, AI 는 결과만 돌려줍니다. → **[docs/API_CONVENTION.md](docs/API_CONVENTION.md)**
- 카테고리 코드 7개: `MEAL` · `CAFE` · `BAR` · `ACTIVITY` · `CULTURE` · `WALK` · `SHOPPING`
- 장소 검색: **카카오 로컬**(좌표 검색 지원). 키가 없으면 가상 데모 데이터.
- 장소 태그: **네이버 블로그 검색**의 제목·요약을 **OpenAI**가 읽고 태그 12개("뷰가 좋아요" 등) 중 해당하는 것을 골라, 글 수가 많은 상위 5개. 사용자 선택 집계는 Spring 이 DB로 처리합니다.
- 코드: AI 로직 `placeflow/v3/` · AI 내부 서버(FastAPI) `placeflow/ai_api/`

## 버전 기록

### v2.1 이후 변경 (2026-11-04) — 캡쳐 인식에 비전 LLM 폴백
워크플로우 버전 표기(`2.1`)는 그대로입니다.
- 캡쳐를 OCR + 규칙 기반으로 먼저 읽고, 다음 경우에만 비전 LLM으로 다시 읽습니다: 장소 없음, 확신도 0.5 미만, 캡쳐마다 다른 상호, OCR 오류.
- `AI_VISION_FALLBACK`·`AI_VISION_FALLBACK_MODEL`(CLI `--vision-fallback`·`--vision-model`)로 켭니다. 기본은 꺼짐입니다.
- LLM 호출이 실패하면 OCR 결과가 있을 때는 그 결과를 쓰고, 없으면 파이프라인 결과는 `failed`입니다.
- 링크·텍스트 입력에는 적용되지 않습니다.

### v2.1 (2026-10-28) — 장소 태그(네이버 블로그 + OpenAI) · 장소 검색 카카오 고정
- **장소 태그**: P03·P04 후보마다 `tags`(`label`·`mentionCount`)·`tagPostCount`, 응답에 `tagSource`를 붙입니다.
  - 태그는 팀이 정한 12개입니다(모든 카테고리 공통): 뷰가 좋아요 · 컨셉이 독특해요 · 사진이 잘나와요 · 차분한 분위기에요 · 야외공간이 멋져요 · 아늑해요 · 혼술하기 좋아요 · 집중하기 좋아요 · 음식이 맛있어요 · 양이 많아요 · 특별한 메뉴가 있어요 · 디저트가 맛있어요. 목록과 LLM 용 설명은 `placeflow/v3/review_tags.json`에 있습니다.
  - '가게 이름 + 동네'로 블로그를 검색해 가게 이름이 나온 글의 제목·요약만 OpenAI(기본 `gpt-4o-mini`, `TAG_LLM_MODEL`)에 보냅니다. 글마다 그 가게를 긍정적으로 말하는 태그만 고르고, 태그별 글 수를 셉니다.
  - 블로그 검색·OpenAI 가 실패해도 인식 결과는 그대로 오고 태그만 비어 있습니다(규칙 기반으로 대신하지 않음). 네이버·OpenAI 키가 없으면 태그 기능이 꺼집니다(`BLOG_TAGS=off`로도 끔).
  - 우리 앱 사용자가 고른 태그 집계(C 방식의 B 단계)는 Spring 이 처리합니다.
- **장소 검색은 카카오로 고정**: 네이버 지역검색 제공자를 뺐습니다(`SEARCH_PROVIDER`: `kakao` · `demo` · `demo_fail`). 네이버 키는 블로그 태그에만 씁니다.
- **해시태그로만 가게를 적은 캡션**도 읽습니다(예: `#잠실맛집 #송리단길 #쿠차라 #배키욘방`). 번호 목록·📍 같은 장소 표시가 없을 때만, 맛집·추천·데이트 같은 일반 태그와 지역 태그를 빼고 남은 해시태그를 장소 이름으로 보고, 지역 태그(#잠실맛집 → 잠실)를 검색어에 붙여 카카오에서 찾습니다. 진짜 고정 댓글은 로그인이 필요해 자동으로 읽지 못하므로 복사해 붙여넣거나 스크린샷으로 넣습니다.
- `/health`에 `tagProvider`를 추가했고, 워크플로우 버전은 `2.1`입니다.

### v2.0 (2026-10-20) — TRD v1.0 반영
- **AI 내부 API를 TRD 형식으로**
  - 경로는 `/internal/v1`이고, P01·P02·P03·P04·R01 짝을 맞췄습니다.
  - JSON camelCase, 성공 DTO 직접 반환, 오류는 `application/problem+json`(`code`·`reason`)입니다.
  - 인식 상태는 `CANDIDATES_FOUND` / `NEEDS_SEARCH`이고, 외부 장애는 503입니다.
  - 이미지는 JPEG·PNG·WebP, 5MiB입니다(413·415). `image` 1장 필수에 `extraImages` 4장까지 받습니다.
- **추천 엔진 v2 (R01)**
  - 입력: `placeCount`(3~5), 방문 순서별 `categorySequence`, 필수 장소 위치 `requiredPlaceOrder`
  - 자리 채우기 방식으로 코스 1~3개를 만들고 `title`·`walkingLegs`·`reason`을 붙입니다.
  - 필수 장소는 카테고리가 달라도 지정 위치에 둡니다.
  - 결과 장소에 `placeId`·`source`(PERSONAL·SHARED·EXTERNAL)를 돌려줍니다.
- **카테고리 7개 코드**를 추가했습니다. 카테고리가 없는 저장 장소는 검색으로 추론합니다(TRD의 DB에 카테고리 필드가 없어서).
- **카카오 로컬 검색**을 추가했습니다(키워드·좌표·카테고리 그룹). `SEARCH_PROVIDER`로 카카오·네이버 중 하나를 고르고, 네이버는 좌표 대신 가까운 지원 지역명으로 우회합니다.
- **P02 좌표 기반 후보 검색**을 만들었습니다(주변 놀거리·교체 후보용). 교체 전용 확장 API도 있습니다.
- **링크는 메타 정보(og:title·og:description)만** 읽습니다(TRD: 본문 크롤링·가격·메뉴 수집 안 함). 본문 읽기는 평가·`/legacy` 전용 코드 옵션으로만 남겼습니다.
- 지역 ID를 TRD 형식(`JAMSIL` 등)으로 바꿨습니다.
- 실험 화면: 장소 수·방문 순서별 카테고리·기준 장소 위치를 고르는 화면을 추가했습니다.
- Spring Boot 백엔드 기준이라 Python 직접 연결(`register`)은 뺐습니다.

### v1.1 (2026-10-16) — 링크 하나에 장소 여러 곳 · 후보 오인 방지 · OCR 개선
- **장소별 묶음 후보**
  - 릴스 '맛집 4곳'처럼 링크에 장소가 여러 곳이면 모두 따로 검색합니다(최대 10곳). 이전에는 앞의 3곳만 검색했습니다.
  - 링크에 나온 이름·순서대로 묶어 보여주고, **여러 곳을 체크해 한 번에 저장**합니다(`POST /saved-places/batch`).
- **확실한 일치 / 비슷한 이름 구분**
  - 캡션 주소의 도로명+건물번호가 같거나, 이름이 같으면(지점명 허용) **확실한 일치**로 보고 기본 체크합니다.
  - 이름 일부만 같거나(오지다 ⊂ 회포차오지다) 주소가 다른 가게는 **비슷한 이름**으로 내려 기본 체크하지 않습니다. 접힌 후보로만, 이유 배지와 함께 보여줍니다.
  - 주소가 없으면 캡션 속 상가·건물 이름과 같은 링크 장소들이 모인 동네를 보조 근거로 씁니다.
- **분류 보강**
  - 통닭·치킨·치맥 → 치킨, 제육·백반·국밥 → 한식, 주꾸미·낙지 → 해산물
  - 가게 이름 안에 끼인 분류어("일**일식**탁")는 무시합니다.
- **OCR 개선**
  - 한 글자씩 띄어 읽던 문제를 고쳤습니다(`preserve_interword_spaces`).
  - 이미지를 확대한 뒤 여러 방식으로 동시에 읽고, 가장 좋은 결과를 고릅니다.
  - 상호명이 깨지면 다른 결과에서 상호명 줄을 가져오고, 그래도 안 되면 주소로 후보를 찾습니다.
- **링크 처리**
  - 네이버 지도 앱의 '공유 → 복사' 문구 전체를 붙여넣을 수 있습니다.
  - robots.txt 로 막힌 링크는 "HTTP 451" 대신 사유와 다음 방법을 안내합니다.
- **네이버 지역검색**: NAVER API HUB(2026-07-31 이후 신규 키 발급처)를 기본으로 씁니다. 개발자센터 기존 키는 `NAVER_SEARCH_ENDPOINT=legacy`로 쓸 수 있습니다.
- **AI 서버(FastAPI)**: 상태 없는 `/ai/v1` API를 처음 만들었습니다(v2.0에서 `/internal/v1` TRD 형식으로 바뀜).
- `docs/API_CONVENTION.md`는 AI 기능만 다루도록 정리했습니다. 보관함·코스 CRUD는 백엔드 소관입니다.

### v1.0 — Figma 와이어프레임 V3 기준 장소 저장·도보 코스 추천
- **후보 확인 저장**: 링크·스크린샷·직접 검색으로 후보를 찾아 확인한 뒤 저장합니다. 하트(저장/해제), 공동 보관함 공유 토글, 내 메모(본인만 조회)를 지원합니다.
- **도보 코스 추천**
  - 반경 1km, 장소 사이 도보 10분 이내, 3~5곳 코스를 서로 다르게 3개 만듭니다.
  - 다시 추천받을 때 이전 코스를 제외하고, 지역을 자동으로 넓히지 않습니다.
  - 실패 상태 5종: 지원하지 않는 지역 · 후보 부족 · 외부 API 실패 · 결과 없음 · 기준 장소 없음
- **코스 상세·편집**
  - 장소별 항목·금액(2인 합계)을 적고, 체크한 항목만 합산합니다.
  - 순서 바꾸기·교체·삭제·추가를 지원하고, 3~5곳 규칙과 저장 시 버전 충돌(409)을 확인합니다.
- **연결 API·규약**: `/api/v3/*` REST 계약과 Python 서비스 계층(`OneClickMeetService`)을 만들고 [docs/API_CONVENTION.md](docs/API_CONVENTION.md)에 정리했습니다.
- **실험 화면**: 모바일형 홈 / 보관함 / 코스 / 마이 탭. 이전 화면은 `/legacy`에 남겼습니다.

### v1 이전 (프로토타입)
- **v0.2**
  - 컨셉 기반 하루 코스 추천(`route.py`, 지금은 legacy)
  - 영업시간 구조화(`hours.py`), 장소 정보 확장(비용·체류시간·예약·평점 등)
  - 캡쳐 여러 장 → 한 장소, Tesseract OCR, robots.txt 준수 크롤링
- **v0.1**: 링크·캡쳐 → 장소 추출·분류 파이프라인, 규칙 기반 추출기, LLM 어댑터, JSON Schema

## 문서
| 문서 | 내용 |
|---|---|
| [docs/API_CONVENTION.md](docs/API_CONVENTION.md) | **AI 내부 API·에러 코드·환경 변수·추천 규칙** |
| [docs/03_product_plan.md](docs/03_product_plan.md) | 현재 제품 범위와 미구현 항목. 부록은 v0.2 기획 기록 |
| [docs/04_data_sources.md](docs/04_data_sources.md) | 데이터 출처별 사용 여부 (링크 메타 정보, OCR·비전 LLM 실측, 도보 시간 추정) |
| [docs/02_workflow.md](docs/02_workflow.md) | 저장 파이프라인 설계 |
| [docs/01_research.md](docs/01_research.md) | 수집 방법 조사와 인스타그램 공식 API 확인 결과 |

## 빠른 시작 (API 키 없이)
```bash
pip install -r requirements.txt
python -m pytest -q                 # 테스트
python eval/run_eval.py             # 평가 → eval/report.md

# 텍스트 한 건
python -m placeflow extract "📍카페 온담 성수점 서울 성동구 서울숲2길 18 2층 딸기케이크 8,500원 #기념일"

# 오프라인 픽스처로 링크 흉내 (가상 데이터)
python -m placeflow extract https://blog.naver.com/fakeblog/220000000002 \
  --fixtures eval/fixtures/pages --geo fixture --trace
```

## AI 내부 서버 실행 (FastAPI · Spring Boot 연동용)
```bash
python3 -m pip install -r requirements-api.txt     # fastapi · uvicorn · python-multipart
python3 -m placeflow api --port 8001               # → http://127.0.0.1:8001/docs (Swagger)
# 배포: uvicorn placeflow.ai_api.main:app --host 0.0.0.0 --port 8001   (AI_SERVICE_KEY 설정 권장)
```
- 엔드포인트·DTO·오류 코드: [docs/API_CONVENTION.md](docs/API_CONVENTION.md), 명세 파일 [docs/openapi.json](docs/openapi.json)
- 장소 검색: `.env`에 `SEARCH_PROVIDER=kakao` + `KAKAO_REST_API_KEY`
- 장소 태그(선택): `.env`에 `NAVER_CLIENT_ID` + `NAVER_CLIENT_SECRET` (NAVER API HUB, 검색 > 블로그) + `OPENAI_API_KEY`
- 스크린샷을 처리하려면 서버에 `tesseract` + 한국어 데이터가 있거나, 비전 LLM 폴백을 켜야 합니다(아래).

## 무료 OCR 설치 (캡쳐 읽기)
```bash
brew install tesseract tesseract-lang     # macOS (한국어 데이터 포함)
tesseract --list-langs | grep kor         # kor 이 보이면 준비 완료
```
설치 후 서버를 다시 켜면 실험 화면의 OCR 선택지에서 'Tesseract'가 기본으로 선택됩니다.

## 비전 LLM 폴백 (선택)
OCR + 규칙 기반으로 추출하지 못한 캡쳐만 이미지를 비전 LLM으로 보냅니다. `.env`에 아래를 넣고 서버를 다시 켭니다.
```bash
AI_VISION_FALLBACK=openai            # openai | anthropic | gemini (비우면 끔)
AI_VISION_FALLBACK_MODEL=gpt-4o-mini
OPENAI_API_KEY=...
```
- 쓰는 제공자의 SDK가 서버를 실행하는 Python에 설치되어 있어야 합니다. OpenAI SDK는 `requirements-api.txt`에 포함되어 있습니다.
- 사용량만큼 요금이 나오고, 장소가 없는 일반 사진도 LLM까지 갑니다.

## 실험용 웹 화면
```bash
python -m placeflow serve          # → 브라우저에서 http://127.0.0.1:8000  (.env 자동 로드)
```
> 실험 화면 코드(`placeflow/server.py`, `placeflow/web/`, `placeflow/v3/api.py`, `placeflow/route.py`)는 `.gitignore`에 들어 있는 로컬 전용 파일입니다. 저장소를 새로 클론하면 이 명령은 동작하지 않습니다.

- **`/` — v1 화면** (모바일형, 홈 / 보관함 / 코스 / 마이)
  - 장소 추가: 링크(또는 앱 공유 문구)·스크린샷·직접 검색 → 인식 중 → 후보 확인(장소별 묶음, 여러 곳 저장) → 실패 화면
  - 보관함: 목록·간이 지도, 하트, 장소 상세(원본 링크·운영시간 안내·내 메모·주변 놀거리)
  - 코스 추천: 기준 선택 → 조건 확인 → 로딩 3단계 → 코스 1/2/3 → 코스 상세(항목·금액·메모) → 편집 → 저장
  - 마이: 검색 제공자(자동·카카오·데모·실패 흉내), 추출기(LLM·OCR), 데모 보관함 채우기, 실험 데이터 초기화
  - 저장 위치: `data/v3.json` (`PLACEFLOW_V3_DB`로 변경 가능)
- **`/legacy` — v0.2 화면**: ① 장소 저장(장소 카드·JSON·실행 단계) / ② 컨셉 코스 추천. 저장 위치는 `data/places.jsonl`
- 127.0.0.1 에서만 열리는 실험용 서버입니다.

## 실제 LLM·지도 API 사용
LLM은 **교체 가능**합니다. `--llm` 으로 고르고, 모델 이름은 `--model` 또는 `PLACEFLOW_LLM_MODEL` 로 넣습니다.
```bash
pip install anthropic            # 또는 openai / google-genai
export ANTHROPIC_API_KEY=...     # .env.example 참고
export KAKAO_REST_API_KEY=...

python -m placeflow extract ./캡쳐.png --llm anthropic --model <모델명> --geo kakao --store data/places.jsonl
python -m placeflow extract "https://blog.naver.com/..." --llm openai --model <모델명> --geo kakao
python eval/run_eval.py --llm gemini --model <모델명>   # 같은 정답셋으로 모델 비교
```
| 설정 | 값 | 설명 |
|---|---|---|
| `--llm` | `heuristic`(기본) · `openai` · `anthropic` · `gemini` | heuristic 은 규칙 기반 기준선, 이미지는 OCR 필요 |
| `--geo` | `none`(기본) · `kakao` · `fixture` | 장소 확정·좌표 보강 |
| `--ocr` | `none` · `tesseract` · `sidecar` | `--llm`이 비전 LLM이 아닐 때 이미지 읽기 |
| `--vision-fallback` · `--vision-model` | `openai` · `anthropic` · `gemini` · 모델 이름 | OCR로 추출하지 못한 캡쳐만 다시 읽기 |
| `--note` | 문자열 | 사용자가 함께 붙여넣은 캡션 (인스타 링크가 막혔을 때 사용) |

새 LLM 을 붙이려면 `extract(system, user_text, images, schema) -> dict` 하나만 구현한 클래스를 `llm/providers.py` 의 `get_provider` 에 등록하면 됩니다.

## 결과 형식 (요약)
```jsonc
{
  "status": "complete",                     // complete | needs_review | needs_user_input | failed
  "records": [{
    "dedupe_key": "kakao:1234",
    "place": { "name": "카페 온담", "branch": "성수점",
               "address": { "road": "서울 성동구 …", "sido": "서울특별시", "sigungu": "성동구" },
               "location": { "lat": 37.54, "lng": 127.04 }, "external_ids": { "kakao_place_id": "1234" } },
    "classification": { "primary_category": "cafe", "sub_category": "dessert",
                        "tags": ["뷰맛집", "기념일"], "free_tags": ["성수카페"] },
    "details": { "signature_menus": [{ "name": "딸기 생크림 케이크", "price": 8500 }], "price_level": 1,
                 "opening_hours_text": "11:00 - 22:00", "closed_days_text": "매주 월요일 휴무" },
    "content": { "summary": "…", "cautions": ["웨이팅 있음"], "evidence": ["…"], "raw_excerpt": "(개인정보 마스킹)" },
    "confidence": { "overall": 0.89, "extraction": 0.8, "geo_match": 1.0 },
    "review_reasons": [],
    "embedding_text": "카페 온담 성수점 | 카페·디저트/디저트 | 서울특별시 성동구 | #뷰맛집 …"
  }],
  "message": null                           // needs_user_input 일 때 사용자 안내 문구
}
```
- 전체 스키마: [schema/place_record.schema.json](schema/place_record.schema.json), 예시: [schema/examples/](schema/examples/)
- 카테고리·태그 정의: [placeflow/taxonomy.py](placeflow/taxonomy.py) (수정 후 `python -m placeflow export-schema`)
- LLM 프롬프트: [placeflow/prompts/extract_system.md](placeflow/prompts/extract_system.md)

## 폴더 구조
```
placeflow/
  router.py        입력 판별 (인스타/네이버 블로그/플레이스/naver.me/이미지/텍스트)
  fetchers/        링크 수집 (HTTP 클라이언트 교체 가능, 오프라인 픽스처 지원)
  llm/             LLM 어댑터 (openai · anthropic · gemini) + 공통 인터페이스
  heuristic.py     LLM 없이 동작하는 규칙 기반 추출기 (기준선·장애 시 대체)
  geo.py           CLI·평가용 장소 확정 + 매칭 점수 (AI 내부 API의 후보 검색은 v3/search.py). 네이버 지역검색 코드가 남아 있지만 쓰지 않음
  pipeline.py      전체 오케스트레이션 · 신뢰도 · 상태
  models.py        데이터 모델 (JSON Schema 의 원본)
  taxonomy.py      카테고리 · 태그 · 가격대
  normalize.py     주소 파싱 · 개인정보 마스킹 · 중복키 · 임베딩 텍스트
  store.py         임시 JSONL 저장소 (중복 병합)
  hours.py         영업시간 파서 · 영업 중 판정
  enrich.py        체류시간·비용·실내 기본값, 필드별 출처
  route.py         (legacy, v0.2, 로컬 전용) 컨셉 하루 코스 추천
  ocr.py           Tesseract(다중 전처리·결과 병합) / 사이드카 OCR
  server.py        (로컬 전용) 실험용 웹 서버 (python -m placeflow serve)
  web/index.html   (로컬 전용) v1 실험 화면   ·  web/legacy.html  v0.2 화면
  ai_api/          AI 내부 서버 (FastAPI, /internal/v1) — main.py · schemas.py(camelCase DTO) · convert.py · errors.py(ProblemDetail) · deps.py · routers/
  v3/              v1 워크플로우 (폴더 이름은 Figma 와이어프레임 V3 기준이라는 뜻)
    service.py     화면 단위 서비스 (백엔드가 import 해서 쓰는 진입점)
    api.py         (로컬 전용) 실험 화면용 /api/v3/* 라우터 · 응답 봉투 · 에러 코드
    save_flow.py   후보 찾기 (장소별 묶음 · 확실한 일치/비슷한 이름 판정)
    recommend.py   추천 엔진 v2 (카테고리 순서·필수 장소 위치) · 교체 후보 · 주변 놀거리
    domain.py      코스·항목 모델과 편집·검증·요약 순수 함수
    search.py      카카오 로컬 · 데모 검색 (SEARCH_PROVIDER)
    blog_tags.py   네이버 블로그 검색(API HUB / 기존 키) → OpenAI 태그 판단 · review_tags.json  태그 12개와 설명
    repo.py        실험용 JSON 저장소 (DB 구현 시 같은 메서드로 교체)
    categories.py  카테고리 코드 7개 · category_infer.py  저장 장소 카테고리 추론
    walking.py     도보 시간 추정 · regions.py  지원 지역 6곳
eval/              평가 정답셋(가상 데이터) · 픽스처 생성기 · 평가 스크립트 · heldout/(검증용 별도 케이스)
schema/            내보낸 JSON Schema · 택소노미 · 예시
tests/             pytest (fixtures/ 실제 캡션 사례)
```

## 알려진 한계
마지막 확인: 2026-11-04

**장소 인식**
- **인스타그램은 사진 안의 장소를 링크만으로 읽지 못합니다.** 서버는 메타 정보만 읽습니다. 사진에만 장소가 있는 게시물은 캡쳐를 올려야 합니다. 비로그인 요청은 차단되거나 응답이 달라질 수 있습니다.
- **인스타그램 자동 수집은 약관 위험이 있습니다.** 인스타그램 `robots.txt`와 약관은 허가 없는 자동 수집을 금지합니다. 사용자가 저장한 링크 1건의 메타 정보만 읽지만, 계속 쓸지는 팀 판단이 필요합니다.
- **`robots.txt` 확인이 User-Agent에 좌우됩니다.** `fetchers/http.py`는 PC용 User-Agent로 읽어 인스타그램 접근을 허용으로 판단합니다. 다른 User-Agent로 받으면 `Disallow: /`나 자동 수집 금지 문구가 나옵니다.
- **OCR은 사진 위에 꾸민 글씨에 약합니다.** 실제 인스타 캡쳐에서 상호명을 통째로 놓친 사례가 있습니다. 이런 캡쳐는 비전 LLM 폴백을 켜야 읽을 수 있습니다.
- **비전 LLM 폴백은 느리고 편차가 큽니다.** 실측한 캡쳐 1장 처리 시간은 약 7~44초였고, 137초 뒤 실패한 경우도 1번 있었습니다(원인 미확인). LLM을 기다리는 동안에도 OCR 동시 처리 슬롯을 차지합니다.
- **LLM 호출 실패가 구분되지 않습니다.** OCR 결과도 없이 LLM이 실패하면 P04는 `NEEDS_SEARCH`로, 실험 화면은 일반 안내 문구로 응답합니다. 실패 사유는 파이프라인 `trace`에만 남습니다.
- **여러 장을 올리면 같은 장소의 화면으로 보는 것이 기본입니다.** 한 요청에 최대 5장입니다. 사진마다 다른 장소가 있는 목록형 게시물은 처리 대상이 아닙니다. 8장짜리 게시물을 한 장씩 돌렸을 때도 오독(`가우동` → `가우뚱`)과 다른 지역 가게로의 연결이 있었습니다.
- 시·도가 없는 주소(`종로구 대학로11길 9-5`)는 규칙 기반 주소 패턴에 걸리지 않습니다.
- 링크는 메타 정보만 읽어서, 블로그 링크만 넣으면 장소를 못 찾는 경우가 늘어납니다. 이때는 공유 문구나 캡션을 붙여넣으면 됩니다.
- 네이버 지도·플레이스 링크는 robots.txt로 막히면 읽지 않습니다. 앱 공유 문구·스크린샷·직접 검색을 씁니다.

**추천**
- 장소 점수는 저장 장소 여부만 봅니다(저장 5점, 외부 0점). 블로그 태그와 영업시간은 추천에 쓰지 않습니다.
- 도보 시간은 직선거리 × 1.3 ÷ 4.5km/h 추정치입니다.
- 지원 지역 6곳의 중심 좌표·반경은 근사값입니다. 기획에서 확정한 경계로 바꿔야 합니다.
- 저장 장소 카테고리 추론은 검색 품질에 좌우됩니다. DB에 카테고리를 저장하면 더 정확합니다.

**태그**
- 태그 판단에 OpenAI(유료)를 씁니다. 장소 1곳당 1회 호출하고, 같은 가게는 24시간 동안 다시 호출하지 않습니다.
- 태그는 네이버의 실제 리뷰 선택 횟수가 아니라 **블로그 제목·요약을 LLM 이 판단한 글 수**입니다. 요약이 짧아서 블로그 글이 적은 가게는 태그가 비거나 적습니다.

**외부 API 확인 범위**
- 카카오 로컬은 실제 키로 키워드 검색을 확인했습니다. 네이버 블로그 태그는 실제 호출에서 `tag_source: naver_blog` 응답을 확인했지만 태그 품질은 평가하지 않았습니다.
- LLM 어댑터는 OpenAI(`gpt-4o-mini`)만 실제 키로 호출했습니다. Anthropic·Gemini는 실제로 호출하지 않았습니다.

**평가**
- **평가셋 10건은 가상 데이터**이고 휴리스틱과 같이 만들어져 100%가 나옵니다(과적합). 이미지 케이스도 정답 OCR 텍스트(sidecar)를 써서 실제 OCR 품질을 반영하지 않습니다.
- 검증 단계에서 따로 만든 held-out 8건(`eval/heldout/`)은 처음에 **78%**(전 필드 일치 7/9)였고, 실패를 고친 뒤에는 8/9입니다. 고친 뒤라 더는 held-out이 아니므로, 실제 캡쳐·링크로 새 정답셋을 만들어야 합니다.

**범위·보안**
- `(확장)` 필드(묶음 `groups`, `placeId`·`source`, `excludeCandidateKeys`, 교체 API 등)는 TRD 초안에 없는 제안이라 서버 담당과 합의가 필요합니다.
- 커플 연결(초대·수락)은 범위 밖입니다.
- 개인정보: 저장되는 `raw_excerpt` 에서 주민번호 형식·이메일·@계정·휴대폰 번호를 가리고, 휴대폰(01x) 번호는 장소 전화번호로 저장하지 않습니다. 비전 LLM 폴백을 켜면 OCR로 추출하지 못한 캡쳐 원본이 외부 LLM으로 전송됩니다.
- 링크 수집기는 사설·루프백 주소로 가는 요청과 리다이렉트를 막습니다(SSRF 기본 방어). 서버 배포 시에는 별도 프록시/허용 목록을 권장합니다.

**로컬 전용 v0.2 화면(`/legacy`, `route.py`)**
- 컨셉 코스 추천은 저장한 장소 안에서만 고르고, 이동 시간은 추정치입니다. 코스 평가는 가상 장소 18곳·시나리오 6개 기준입니다.
- 네이버 플레이스 홈 탭에는 영업시간이 일부만 보여서, 요일별 영업시간은 '정보' 탭 캡쳐가 있어야 정확합니다.
