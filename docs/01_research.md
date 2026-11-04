# 데이터 수집 방법 조사

> 최초 조사 2026-10-07, 인스타그램 부분 갱신 2026-11-04. 사실(출처 있음)과 추론(⚠️ 표시)을 구분했습니다.
> 1·2장은 조사 당시 기록입니다. 현재 설계는 3장과 [04_data_sources.md](04_data_sources.md)를 따릅니다.

## 1. 입력 출처별 수집 가능성

| 입력 | 수집 방법 | 가능 여부 | 제약 | 근거 |
|---|---|---|---|---|
| 인스타그램 게시물/릴스 링크 | 공식 oEmbed (`/instagram_oembed`) | △ | Meta 앱 + `meta_oembed_read` 기능 필요. 2025년부터 `thumbnail_url`, `author_name` 미반환. 반환 필드는 `html`, `provider_name` 등이며 캡션을 별도 필드로 준다는 명시 없음 | [Meta oEmbed 업데이트](https://developers.facebook.com/blog/post/2025/04/08/oembed-updates/), [Instagram oEmbed 문서](https://developers.facebook.com/docs/instagram-platform/oembed) |
| 〃 | 페이지 HTML의 `og:title`/`og:description` 메타태그 | △ | ⚠️ 비로그인 요청은 로그인 벽·차단에 자주 막힘. Meta 스스로 "게시물 HTML 메타데이터에서 직접 썸네일을 생성하라"고 안내함 | 위 Meta 블로그 |
| 〃 | 비공식 스크래핑(Apify 등) | ✗ (권장 안 함) | 약관 위반 위험, 서비스 품질 불안정 | — |
| 〃 | **캡쳐 이미지 / 캡션 붙여넣기** | ○ | 가장 안정적인 경로 → 인스타는 **캡쳐 우선** 설계 | 추론 |
| 네이버 블로그 링크 | `m.blog.naver.com/{blogId}/{logNo}` 모바일 페이지 HTML + OG 메타 | ○ | ⚠️ PC 페이지는 본문이 iframe(`PostView.naver`) 안에 있으므로 모바일 URL로 바꿔서 가져오는 것이 단순함 | 추론(일반 관행) |
| 네이버 플레이스/지도 링크 | URL에서 place ID 추출 + OG 메타(상호명) | ○ | ⚠️ 상세 정보 페이지는 동적 렌더링 → 상호명만 얻고 **지역검색 API로 보강** | 추론 |
| `naver.me` 단축 링크 | HTTP 리다이렉트를 따라가 원래 URL 확인 | ○ | 리다이렉트 결과로 다시 라우팅 | 추론 |
| 캡쳐 이미지 | 비전 LLM(멀티모달) 또는 OCR(Tesseract 등) | ○ | OCR은 한글 정확도가 들쭉날쭉 → **비전 LLM 우선, OCR은 대안** | 추론 |

## 2. 장소 확정(보강)용 API

### 네이버 지역 검색 API (조사 기록, 현재 사용 안 함)
- 파라미터: `query`(필수), `display` 1~5(기본 1), `start`, `sort`=`random`(정확도)·`comment`(리뷰 수)
- 응답: `title`, `link`, `category`, `description`, `telephone`, `address`(지번), `roadAddress`, `mapx`, `mapy`
- 좌표: WGS84, `mapx`=경도, `mapy`=위도 (현재 문서 기준. ⚠️ 구 버전 응답은 정수 스케일(×10⁷) 형태도 있어 코드에서 둘 다 처리)
- 하루 호출 한도: 25,000회
- 출처: [NAVER API Hub – 지역 검색](https://api.ncloud-docs.com/docs/naver-api-hub-search-local)

### 카카오 로컬 API (키워드 장소 검색)
- `GET https://dapi.kakao.com/v2/local/search/keyword.json`
- 파라미터: `query`, `x`, `y`, `radius`, `size`(최대 15), `page`(최대 45), `category_group_code`
- 응답: `id`, `place_name`, `category_name`, `category_group_code`, `phone`, `address_name`, `road_address_name`, `x`(경도), `y`(위도), `place_url`
- 카테고리 그룹 코드(데이트 관련): `FD6` 음식점, `CE7` 카페, `CT1` 문화시설, `AT4` 관광명소, `AD5` 숙박
- 무료 쿼터는 개발자 계정당 처음 활성화한 앱에만 제공, 초과분은 유료
- 출처: [Kakao Local 공통](https://developers.kakao.com/docs/en/local/common), [Kakao Map REST API](https://developers.kakao.com/docs/en/kakaomap/rest-api)

## 3. 현재 설계 결론
1. **인스타그램 = 캡쳐/캡션 우선**. 링크는 메타 정보를 시도하고, 실패하면 캡쳐를 요청합니다.
2. **네이버 블로그 = 메타 정보만**. 처음에는 본문을 수집했지만 TRD에 따라 바꿨습니다. 본문 읽기는 평가·`/legacy` 전용 옵션입니다.
3. **네이버 플레이스 = place ID와 메타 정보** → 후보 검색으로 보강합니다.
4. **장소 검색 = 카카오 로컬 고정** (v2.1). 네이버 지역검색은 쓰지 않고, 네이버 API는 블로그 태그에만 씁니다.
5. **캡쳐 = OCR 우선, 실패 시 비전 LLM**. 처음 결론(비전 LLM 우선)에서 비용을 고려해 바꿨습니다.
6. LLM이 만든 주소는 **지도 API 결과와 일치할 때만 확정**하고, 아니면 신뢰도를 낮추고 사용자 확인 필요로 표시합니다.

## 4. 인스타그램 공식 API 확인 (2026-11-04)
결론: **공유받은 임의 게시물의 사진을 읽는 용도로 쓸 수 있는 공식 API는 확인하지 못했습니다.**

| API | 확인 내용 | 판단 |
|---|---|---|
| [Instagram Platform](https://developers.facebook.com/docs/instagram-platform/overview/) | 프로페셔널 계정(비즈니스·크리에이터) 데이터 접근용 | 임의 게시물 조회에 부적합 |
| [Business Discovery](https://developers.facebook.com/docs/instagram-api/business-discovery) | 다른 비즈니스·크리에이터 계정의 공개 데이터를 **계정 이름으로** 조회. Facebook Login과 권한 필요. 얻은 미디어 ID로 직접 조회는 권한 오류 | 개인 계정 불가로 보임. 여러 장 게시물의 사진 조회 가능 여부는 실제 호출로 확인 필요 |
| [oEmbed](https://developers.facebook.com/blog/post/2025/04/08/oembed-updates/) | `thumbnail_url` 등 제거 | 사진을 얻는 용도로 사용 불가 |
| Basic Display | [2024-12 종료](https://www.getphyllo.com/post/instagram-api-guide) (제3자 자료) | 사용 불가 |

인스타그램 [고객센터](https://help.instagram.com/740480200552298)와 `robots.txt`는 허가 없는 자동 수집을 금지합니다. 앱 심사 요건은 이번에 확인하지 못했습니다.
