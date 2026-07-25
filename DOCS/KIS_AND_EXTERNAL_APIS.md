# 한국투자증권·시장데이터·AI 연동

- 문서 등급: 외부 연동 기준
- 활성 주문 공급자: 한국투자증권 Open API(KIS)
- 확인 기준일: 2026-07-26

키움증권 REST API는 활성 구현 기준이 아니다. 전환 결정과 차이는 [`BROKER_API_COMPARISON.md`](BROKER_API_COMPARISON.md)에 둔다.

## 1. 사용자가 준비할 것

### 필수

1. 한국투자증권 국내주식 거래 가능 계좌를 개설한다. 비대면 개설 시 실제 적용 수수료도 확인한다.
2. 한국투자증권 온라인 ID/HTS ID를 만들고 계좌를 연결한다.
3. [KIS Developers](https://apiportal.koreainvestment.com/)의 `API신청`에서 Open API 서비스를 신청한다.
4. 실전투자용 `App Key`와 `App Secret`을 발급받는다.
5. 모의투자용 `App Key`와 `App Secret`을 별도로 발급받는다.
6. 실전 계좌번호와 모의 계좌번호를 확인한다. 설정에는 앞 8자리와 뒤 2자리 상품코드를 분리한다. 일반 종합계좌 상품코드는 보통 `01`이지만 본인 계좌에서 확인한다.
7. 체결통보와 일부 WebSocket 기능에 사용하는 HTS ID를 확인한다.
8. 모의투자에서 현재가·잔고·주문·정정/취소·매도와 WebSocket 체결·호가를 시험할 수 있는지 확인한다.

### 프로젝트 운영에 필요

9. GitHub 저장소를 준비하고 Pages를 활성화한다. 저장소가 공개인지 비공개인지 결정한다.
10. AI 공급자와 모델을 결정하고 API Key와 월 비용 한도를 준비한다.
11. 상시 감시용 호스트를 결정한다. 개발은 현재 노트북으로 가능하지만, 실전 포지션 보유 중 절전·종료되지 않아야 한다.
12. SMTP 발신계정, 앱 비밀번호, 수신주소를 확인한다. 기존 프로젝트에서 가져온 설정은 실제 시험메일로 검증한다.

현재 KIS 공식 샘플의 준비 항목에는 키움식 공인 IP 등록이 포함되어 있지 않다. 따라서 공인 IP 등록을 선행 필수조건으로 두지 않되, 서비스 신청 화면과 약관에 계정별 추가 조건이 표시되면 그 조건을 따른다.

## 2. 사용자 전달 금지 원칙

사용자는 App Key·App Secret·계좌번호·HTS ID를 채팅에 붙여 넣지 않는다. 에이전트가 생성한 Git 제외 로컬 비밀 파일 또는 OS 자격증명 저장소에 사용자가 직접 입력한다.

권장 경로:

```text
.secrets/
  kis/paper.json
  kis/prod.json
  imported_financial_statement_analysis/key.txt
  imported_financial_statement_analysis/email_config.json
```

JSON 필드:

```json
{
  "environment": "paper",
  "app_key": "",
  "app_secret": "",
  "account_no": "",
  "product_code": "01",
  "hts_id": ""
}
```

모의와 실전은 파일·프로세스·토큰 캐시·DB 스키마 또는 DB 인스턴스를 분리한다. 실행 시 `KIS_ENV`와 키·계좌의 환경이 일치하지 않으면 시작을 거부한다.

## 3. 공식 샘플 기준 환경

| 환경 | REST 기본 주소 | WebSocket 기본 주소 |
| --- | --- | --- |
| 실전 | `https://openapi.koreainvestment.com:9443` | 공식 설정의 `ops` 값 사용 |
| 모의 | `https://openapivts.koreainvestment.com:29443` | 공식 설정의 `vops` 값 사용 |

WebSocket 주소와 TR ID는 코드에 중복 하드코딩하지 않고 버전 관리되는 KIS 설정과 어댑터에 둔다. 공식 샘플은 `prod`와 `vps`로 실전·모의를 전환하며, REST 접근토큰과 WebSocket 접속키를 별도로 발급한다.

- 접근토큰은 응답의 만료시각을 기준으로 캐시하고 단일 잠금으로 갱신한다.
- 공식 샘플 안내상 토큰 재발급은 1분당 1회이므로 프로세스마다 무분별하게 발급하지 않는다.
- 모든 요청은 `tr_id`, 연속조회 키, 환경을 감사 가능한 메타데이터로 남긴다.
- App Key, App Secret, 토큰, 접속키, 전체 계좌번호는 로그에 남기지 않는다.

## 4. KIS `provider-doctor`

구현 전에 실제 사용자 계정으로 다음을 순서대로 진단하고 결과를 `provider_capability_snapshot`으로 저장한다.

1. 모의·실전 App Key가 올바른 환경에서 토큰을 발급하는지
2. 계좌번호 앞 8자리·상품코드·HTS ID가 올바른지
3. 국내주식 현재가·일봉·분봉·거래량·거래대금 조회
4. 실시간 체결·호가 WebSocket 구독과 재연결
5. 계좌 잔고·매수가능금액·매도가능수량 조회
6. 모의 현금 매수, 주문조회, 정정·취소, 현금 매도
7. 부분체결·미체결·체결통보와 REST 조회 대조
8. 프로세스 재시작 후 잔고·주문·체결 복구
9. -7% 시장가 손절과 적응형 익절 모의 시나리오
10. 실제 계정의 REST 호출한도·WebSocket 구독한도·오류코드·지연 측정
11. 소액 실전 조회
12. 사용자 별도 승인 후 소액 실전 매수·매도

공식 샘플은 모의투자 REST 호출 제한이 낮고 연속 호출 시 `EGW00201`이 발생할 수 있다고 안내한다. 한도 숫자를 추정해 문서에 고정하지 않고 `provider-doctor`가 실제 허용량을 측정하되, 오류를 유발하는 공격적 부하 시험은 하지 않는다.

## 5. KIS 사용 범위

| 기능 | KIS 역할 | 구현 원칙 |
| --- | --- | --- |
| 인증 | REST 토큰·WebSocket 접속키 | 캐시·단일 갱신·환경 분리 |
| 시장 조회 | 현재가·일/분봉·체결·호가 | 후보 배치는 캐시·KRX와 분산 |
| 실시간 | 지정 1~3개 체결·호가·체결통보 | 재연결·신선도·REST 대체 |
| 계좌 | 잔고·매수/매도 가능수량 | 내부 원장의 최종 대조 기준 |
| 주문 | 현금 매수·매도·정정·취소 | 사용자 승인·멱등성·감사 |

주문 TR ID와 필드명은 KIS 공식 API 문서와 공식 GitHub 샘플에서 가져오고 어댑터 계약 테스트로 고정한다. 공식 샘플 변경을 자동으로 실전에 반영하지 않는다.

## 6. 데이터 공급자 역할

| 공급자 | 역할 | 주문 |
| --- | --- | --- |
| KIS | 시세·일/분봉·체결·호가·계좌·주문 | 유일한 활성 주문 공급자 |
| Open DART | 공시·기업코드·위험 이벤트 | 없음 |
| KRX | 종목·시장통계·수급·공매도 보조검증 | 없음 |

- 주문·잔고·체결은 KIS가 최종 기준이다.
- 공시는 DART 접수번호와 원문이 기준이다.
- 시장 상태는 거래 직전 KIS를 우선하고 KRX로 교차검증한다.
- 충돌값을 평균하지 않고 공급자·시각·차이를 기록해 후보 또는 신규주문을 차단한다.
- KRX·KIS 원시 데이터의 GitHub Pages 재배포는 각 이용정책을 확인한다.

후보 30개 생성을 위해 전 종목을 장중 KIS REST로 반복 조회하지 않는다. 일별 배치 데이터는 KRX·DART·저장된 일봉으로 만들고, KIS는 거래 직전 시세와 지정 종목 실시간 감시, 계좌·주문에 우선 사용한다.

### 일일 후보 배치의 공급자 경계

1. KRX 일괄 시세로 최근 21거래일 KOSPI 전 종목의 일봉·거래량·거래대금을 수집한다.
2. 7·14·21거래일 박스와 변동성·완전 왕복·유동성을 같은 원시 일봉에서 계산한다.
3. KRX 투자자별 순매수로 개인·외국인·기관·금융투자·연기금의 기간 합계를 계산한다.
4. 정량 점수로 30개를 고른 뒤에만 KIS 현재가·일봉으로 신선도와 가격을 교차검증한다.
5. KIS 교차검증 실패 또는 공급자 간 가격 차이가 허용범위를 넘으면 해당 후보의 신규 진입을 차단한다.

KRX 접근 구현은 `pykrx` 어댑터를 사용하되 제3자 라이브러리 자체를 공식 API로 간주하지 않는다. KRX 응답 구조 변경이나 빈 응답을 정상적인 0으로 바꾸지 않고 배치를 실패시킨다. 원시 데이터와 생성 보고서에는 공급자·기준 거래일·산식 버전을 기록한다.

KIS 일봉 교차검증은 공식 샘플의 `inquire-daily-itemchartprice`, 경로 `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`, TR ID `FHKST03010100`을 사용한다. 종목별 투자자 일별 자료가 필요할 때는 공식 샘플의 `investor-trade-by-stock-daily`, 경로 `/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily`, TR ID `FHPTJ04160001`을 사용한다. 호출 필드와 응답 계약은 공식 샘플 기준 계약 테스트로 고정한다.

## 7. AI 에이전트 연동

AI는 매일 정량 후보 30개 전부를 검토하고 각 종목에 4단계 등급과 코멘트를 부여한다. 입력은 정량 특징, 박스 이동, 수급, 공시, 시장·업종, 데이터 품질이며 계좌·키·보유금액을 포함하지 않는다.

출력:

- `적극 추천/추천/비추천/적극 비추천`, 최종 순위
- 근거·위험·무효화 조건
- 데이터 시각, 모델 ID, 프롬프트 버전, 입력 해시
- 검증 가능한 JSON

AI는 30개 밖의 종목을 추가하거나 원시 숫자를 수정할 수 없다. 실시간 손절·익절은 외부 AI를 기다리지 않고 로컬 엔진이 실행한다. KIS 공식 LLM 예제·전략 빌더·백테스터는 참고 구현으로 사용하되, 프로젝트의 사용자 승인 규칙과 -7% 손절 정책을 대체하지 않는다.

## 8. 준비 상태

| 항목 | 상태 |
| --- | --- |
| DART API Key | 기존 프로젝트에서 안전 경로로 복사됨, 실제 호출 재검증 필요 |
| KRX 계정 정보 | 기존 프로젝트에서 안전 경로로 복사됨, 실제 호출 재검증 필요 |
| SMTP 설정 | 기존 프로젝트에서 안전 경로로 복사됨, 시험메일 필요 |
| KIS 모의 App Key·Secret | 입력 완료, 2026-07-26 live doctor 통과 |
| KIS 실전 App Key·Secret | 사용자 준비 완료, `.secrets/kis/prod.json` 입력 가능·실전 실행 잠금 |
| KIS 실전·모의 계좌번호·상품코드 | 모의 계좌 검증 통과, 실전 실행 잠금 |
| KIS HTS ID | WebSocket 접속키 검증 통과 |
| AI API Key·모델·비용 한도 | 사용자 준비 완료로 전달됨, 실제 연동 시 검증 |
| GitHub 저장소·Pages 권한 | 사용자 준비 완료로 전달됨, Phase 2에서 검증 |
| 상시가동 호스트 | 사용자 준비 완료로 전달됨, 실전 승격 전 장애검증 |

## 9. 공식 자료

### 한국투자증권

- [KIS Developers 포털](https://apiportal.koreainvestment.com/)
- [Open API 서비스 이용안내](https://apiportal.koreainvestment.com/about-howto)
- [Open API API 문서](https://apiportal.koreainvestment.com/apiservice-apiservice)
- [공식 Open API GitHub](https://github.com/koreainvestment/open-trading-api)
- [공식 AI 에이전트 확장 저장소](https://github.com/koreainvestment/kis-ai-extensions)
- [한국투자증권 수수료 안내](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp)
- [키움증권 비교](BROKER_API_COMPARISON.md)

### DART·KRX

- [Open DART 소개](https://opendart.fss.or.kr/intro/main.do)
- [Open DART 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001)
- [KRX Data Marketplace](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd)
- [KRX 정보 이용정책](https://data.krx.co.kr/inc/datasale/Market%20Data%20Usage%20Polices_ko.pdf)

### GitHub

- [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Pages 배포](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)
- [예약 Actions 지연 주의](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)

블로그보다 공식 자료를 먼저 확인하고 필드·TR ID·호출한도를 추정하지 않는다.
