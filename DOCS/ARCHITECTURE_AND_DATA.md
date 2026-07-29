# 시스템 아키텍처와 데이터 계약

## 1. 목표

- 일별 후보 배치와 실시간 주문 경로를 분리한다.
- 외부 AI 지연이 손절·익절을 막지 않게 한다.
- AI 에이전트는 감독·분석·정책 제안 역할을 맡고, 상시 실행과 보호 주문은 로컬 결정 엔진이 맡는다.
- 재시작 후 KIS 잔고·주문·체결 조회에서 상태를 복구한다.
- GitHub Pages와 실계좌 인프라를 보안 경계로 분리한다.
- KIS 필드·TR ID·환경별 한도 변경을 어댑터에 격리한다.

```mermaid
flowchart LR
    subgraph EXT["외부"]
      KIS["KIS REST·WebSocket"]
      DART["Open DART"]
      KRX["KRX"]
      AI["AI API"]
      SMTP["SMTP"]
      GH["GitHub Pages"]
    end
    subgraph CORE["항상 켜진 운영 호스트"]
      COL["수집기"]
      BATCH["후보 엔진"]
      REVIEW["AI 검토기"]
      ORCH["운영 오케스트레이터"]
      MON["종목별 비동기 모니터"]
      ENTRY["진입 엔진"]
      OMS["주문·포지션 관리자"]
      RISK["위험 엔진"]
      REGIME["시장위험 가드"]
      BUS["이벤트 버스·Outbox"]
      NOTI["알림"]
      PUB["정적 빌더"]
      API["FastAPI"]
      DB[("PostgreSQL")]
    end
    KIS --> COL
    DART --> COL
    KRX --> COL
    COL --> DB
    DB --> BATCH --> REVIEW
    AI --> REVIEW
    REVIEW --> DB
    API --> ORCH
    DB --> ORCH --> MON
    KIS <--> MON
    MON --> BUS
    BUS --> ENTRY --> ORCH
    BUS --> RISK --> ORCH
    REGIME --> ORCH
    ORCH --> OMS
    OMS <--> KIS
    OMS --> DB
    DB --> NOTI --> SMTP
    DB --> PUB --> GH
```

## 2. 서비스 책임

| 서비스 | 책임 |
| --- | --- |
| `provider-doctor` | KIS 실전·모의 인증, 계좌, REST, WebSocket, TR·호출한도 진단 |
| 시장데이터 수집기 | 종목·수급·공시·일봉 보조자료와 정규장 1분봉 원본의 증분 수집·품질 검사 |
| 재무 스냅샷 수집기 | Open DART 다중회사 주요계정으로 최소 재무항목을 독립 수집하고 파생 비율·위험 플래그·접수번호를 버전 저장 |
| 봉 집계기 | 1분봉 원본에서 재현 가능한 5·10·30·60분봉 OHLCV 생성 |
| 후보·AI 엔진 | 시총 상위 200개 계산·전체 표시, 정량 상위 50개 AI 심층검토, +10% 자격 게이트 통과 공식 주문 후보 최대 30개와 버전 저장 |
| 운영 오케스트레이터 | [`TRADING_ORCHESTRATOR.md`](TRADING_ORCHESTRATOR.md)에 따라 종목별 actor 생성·복구·중지, 계좌 자금 예약, 주문 우선순위와 진입/보호 작업 분리 |
| 실시간 모니터 | 현재 승인된 최대 3개, 향후 검증된 `N`개 종목의 체결·호가·1분/5분 상태를 비동기 actor로 계산 |
| 승인 게이트웨이 | 사용자 진입 위임 검증과 일회용·만료형 주문 범위 |
| 주문 관리자 | 주문·정정·취소·체결·잔고 대조 |
| 위험 엔진 | -7% 손절, 적응형 익절, 시간·계좌 한도 |
| 시장위험 가드 | 손절 후 종목·업종·시장·시스템 원인을 분류하고 진입 회로 차단 |
| 이벤트 버스·Outbox | 워커·AI·대시보드 간 내구성 있는 이벤트 전달과 재처리 |
| 알림·대시보드 | SMTP 큐와 정제된 정적 페이지 |
| 정적 리포트 빌더 | 200개 공개 DTO와 공식 주문 후보 최대 30개를 분리 검증하고 7·14·21일 지표와 자체 차트 생성 |

재무 스냅샷은 `financial_statement_analysis-main`의 Excel이나 내부 모듈을 읽지 않는다. `domain/fundamentals.py`가 공급자 비의존 계약과 파생 산식을 소유하고, `adapters/dart/financials.py`가 Open DART 응답을 내부 계정으로 변환하며, `services/fundamental_snapshot.py`가 시총 상위 200개 갱신·캐시·보고서 결합을 담당한다. 재무 결측은 가격·분봉 파이프라인의 폴백 값으로 채우지 않는다.

권장 스택은 Python 3.11+, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL, asyncio/httpx/WebSocket, pytest/hypothesis, 구조화 로그다.

## 3. 배포 경계

- 개발: 현재 노트북, KIS 모의투자, 로컬 DB
- 실전: 상시가동 호스트와 안정적인 네트워크 권장
- GitHub: 코드와 정제된 후보 정적 데이터만
- `.secrets/`: 키·메일·계좌 매핑, Git 제외

GitHub Actions 예약 작업은 시장 감시와 주문에 사용하지 않는다. 운영 호스트가 배치를 실행하고 GitHub는 정적 배포만 담당한다.

## 4. 의존성

```text
domain <- application <- adapters <- entrypoints
```

- 손절·익절은 KIS TR ID·응답 코드에 의존하지 않는 순수 도메인 로직이다.
- KIS 응답은 `KisBrokerAdapter`에서 내부 모델로 변환한다.
- 주문 의도와 브로커 결과를 별도 이벤트로 저장한다.
- 이메일·GitHub 장애는 주문 엔진을 중단시키지 않는다.
- AI 프로세스가 종료돼도 열린 포지션의 손절·익절·잔고대조는 계속 실행된다.

### 자동매도 모듈 경계

자동매도는 매수·대시보드·일일 후보 스크립트에 조건문으로 흩어놓지 않는다. 다른 자동화가 재사용할 수 있도록 다음 경계로 분리한다.

| 위치 | 책임 | 금지 |
| --- | --- | --- |
| `domain/risk.py` 또는 향후 `domain/risk/` 패키지 | 손익 구간, 순수 점수·상태전이, `ExitDecision` 생성 | KIS 호출, DB, 이메일 |
| `adapters/kis/realtime.py` | KIS 체결·호가·시장 이벤트를 내부 모델로 변환 | 손절 정책 판단 |
| `services/risk_engine.py` | 포지션별 상태, 시간창 지표, 정책 호출, 복구 | 브로커 필드 직접 해석 |
| `services/market_regime_guard.py` | 정상·업종 위험·시장 위험·패닉 판정과 신규매수 차단 | 개별 주문 직접 제출 |
| `services/order_manager.py` | 멱등 `ExitIntent`, 전량 시장가 주문·부분체결·잔고 대조 | 임의 전략 변경 |
| `services/risk_replay.py` | 저장된 실제 사례를 같은 정책 버전으로 재생·비교 | 실계좌 주문 |

현재 `domain/risk.py`의 평균 체결가·`-7%` 가격·발동 함수는 순수 핵심으로 유지한다. 단계형 방어 로직을 구현할 때 파일 하나가 비대해지면 동일 공개 API를 유지한 `domain/risk/` 패키지로 분리한다. 호출자는 내부 파일이 아니라 `RiskEngine.evaluate(snapshot) -> ExitDecision` 계약에만 의존한다.

### 자동매수 모듈 경계

자동매수도 후보 선정·대시보드·자동매도 스크립트에 섞지 않는다. 상세 정책은 [`ENTRY_AND_BUY.md`](ENTRY_AND_BUY.md)를 단일 기준으로 삼는다.

| 위치 | 책임 | 금지 |
| --- | --- | --- |
| `domain/entry.py` 또는 향후 `domain/entry/` 패키지 | 최대 매수가, 순수 게이트·점수·상태전이, `EntryDecision` | KIS 호출, DB, 이메일 |
| `services/entry_engine.py` | 종목별 시간창·진입 상태, 정책 실행, 재시작 복구 | 브로커 원시 필드 직접 해석 |
| `adapters/kis/realtime.py` | KIS 체결·호가·시장 이벤트를 내부 모델로 변환 | 진입 정책 판단 |
| `services/market_regime_guard.py` | 시장·업종 위험과 신규매수 차단 | 개별 주문 직접 제출 |
| `services/order_manager.py` | 멱등 `EntryIntent`, 상한 이하 지정가·부분체결·취소·잔고 대조 | 전략 임계값 변경 |
| `services/entry_replay.py` | 실제 장중 사례를 같은 진입정책 버전으로 재생·비교 | 실계좌 주문 |

호출자는 `EntryEngine.evaluate(snapshot, mandate, policy) -> EntryDecision` 계약에만 의존한다. 자동매수와 자동매도는 주문 관리자·시장 위험 가드·내부 시장 스냅샷을 공유하지만 정책 상태는 분리한다.

### 다종목 실행 코어

자동매수·자동매도 엔진 위의 중앙 조정 계층은 [`TRADING_ORCHESTRATOR.md`](TRADING_ORCHESTRATOR.md)를 단일 기준으로 삼는다. 종목별 `SymbolActor`가 순수 결정을 만들고 `TradingOrchestrator`가 계좌 자금·시장 위험·주문 충돌·우선순위를 판정해 중앙 `OrderManager`로 전달한다. 현재 3개 제한은 승인 정책이며 코드 구조의 고정 배열 크기가 아니다.

## 5. 핵심 데이터

| 테이블 | 역할 |
| --- | --- |
| `instruments`, `daily_bars`, `intraday_bars` | 종목·일봉 보조자료·정규장 1분봉 원본과 파생 5/10/30/60분봉 |
| `ticks`, `orderbooks` | 체결·호가 원본 또는 보존본 |
| `investor_flows`, `disclosures` | 수급·공시 |
| `box_features` | 박스·일중 변동성·다중 목표 상승 탄력성·하단 방향 버전형 특징 |
| `candidate_runs`, `candidates` | 정량·AI 후보와 근거 |
| `universe_memberships`, `intraday_coverage` | 사전필터 편입·이탈 이력, 공통 수집 시작일, 종목별 목표·완료·누락 거래일과 체크포인트 |
| `candidate_window_metrics` | 후보별 7·14·21일 박스·진폭·위치·수급·차트 입력 |
| `candidate_publications` | 정제된 공개 DTO 해시·생성시각·배포상태·Pages URL |
| `user_comments`, `remote_directives`, `watch_selections` | 대시보드 코멘트, Codex 지시 ID, 사용자 지정 1~3개 |
| `entry_mandates` | 종목·목표가·사용자 배정률·계좌모드·정책버전·사용자 취소 상태 |
| `ai_policy_decisions` | 유효기간이 있는 AI `ALLOW/BLOCK`·근거·입력 해시 |
| `order_intents`, `broker_orders`, `fills` | 주문 의도·KIS 주문·체결 |
| `positions`, `risk_events` | 포지션 세대와 손절·익절 판단 |
| `ai_usage_snapshots`, `ai_invocations` | Codex 잔여량 수기 스냅샷과 API 토큰·비용 |
| `assurance_runs`, `assurance_checks` | 일일 품질 실행·개별 체크 판정·증거·신규매수 허용 상태 |
| `strategy_versions`, `promotion_decisions` | 실전 안정판·모의 그림자·개선판 버전과 승격·롤백 승인 |
| `live_paper_divergences` | 실전·모의 신호·주문·체결·슬리피지 차이 |
| `notifications`, `audit_log` | 발송 상태와 감사 |

## 6. 이벤트

주요 이벤트는 다음과 같다.

```text
DAILY_DATA_READY, QUANT_CANDIDATES_CREATED, AI_REVIEW_COMPLETED,
DASHBOARD_PUBLISHED, USER_COMMENTED, CODEX_USER_DIRECTIVE_RECEIVED,
UNIVERSE_MEMBER_ADDED, INTRADAY_CATCHUP_REQUESTED,
INTRADAY_CATCHUP_COMPLETED, INTRADAY_CATCHUP_FAILED,
COMMAND_NORMALIZED, WATCHLIST_SELECTED,
ENTRY_MANDATE_APPROVED, LOWER_BOUND_APPROACHED, REVERSAL_CONFIRMED,
ENTRY_SIGNAL_CONFIRMED, BUY_ORDER_SUBMITTED,
ORDER_PARTIALLY_FILLED, POSITION_OPENED, HARD_STOP_TRIGGERED,
PROFIT_MODE_ARMED, PROFIT_FLOOR_RAISED, PROFIT_EXIT_TRIGGERED,
TIME_EXIT_TRIGGERED, POSITION_CLOSED, PROVIDER_DEGRADED,
STOP_CONTEXT_ASSESSMENT_REQUESTED, SYMBOL_QUARANTINED,
MARKET_RISK_OFF_ENTERED, ACCOUNT_RECONCILIATION_FAILED,
AI_BUDGET_YELLOW, AI_BUDGET_RED, AI_REVIEW_QUOTA_DEGRADED,
ASSURANCE_RUN_STARTED, ASSURANCE_CHECK_FAILED,
ASSURANCE_REPORT_CREATED, DAILY_ASSURANCE_SENT,
LIVE_PAPER_DIVERGENCE_DETECTED, CHALLENGER_PROMOTION_PROPOSED,
STRATEGY_VERSION_APPROVED, STRATEGY_VERSION_ROLLED_BACK
```

모든 이벤트는 `event_id`, 종류, 발생·기록시각, 상관·원인 ID, 계좌 별칭, 종목, 스키마 버전, payload를 가진다.

## 7. 멱등성·시간·정밀도

- 청산 후 재진입은 새 `position_generation`을 만든다.
- 주문 키는 `account + symbol + generation + action + cause`로 만든다.
- 타임아웃 시 재전송 전에 KIS 주문·미체결·체결을 조회한다.
- 가격·금액은 정수 원 또는 `Decimal`을 사용한다.
- 거래소 시각과 수신 시각을 모두 보존한다.
- 모든 피처에 `as_of`, `data_version`, `calculation_version`을 둔다.
- 구조 피처에는 `source_bar_interval`, `analysis_bar_interval`, 원본 해시, 집계 버전, 세션 완성도와 결측 수를 추가한다.
- 후보 실행에는 `prefilter_version`, 종목별 `available_trading_days`, 활성 기간 목록과 백필 체크포인트를 기록한다.
- 기간 버튼 가용성과 구조 필드 가용성을 분리한다. 일별 참고 지표는 표시할 수 있어도 분봉 구조 지표의 `WARMING_UP` 상태는 해제하지 않는다.
- 파생봉은 별도 공급자의 서로 다른 원본으로 섞지 않고 같은 1분봉에서 재생성할 수 있어야 한다.
- 구조 피처의 기본 `analysis_bar_interval`은 60분이며 30분·10분은 별도 실험 버전으로 격리한다.

## 8. 공개와 보존

- 주문·체결·위험·감사 로그는 장기 보존한다.
- 틱·호가는 용량과 데이터 라이선스에 따라 보존기간을 정한다.
- GitHub Pages에는 정제된 후보 공개 필드만 보낸다.
- 계좌·승인·주문·포지션은 정적 산출물에 포함하지 않는다.

## 9. 동시성 모델

종목 3개를 위해 OS 스레드를 종목마다 무조건 생성하지 않는다. 기본은 하나의 `asyncio` 이벤트 루프, 공유 KIS WebSocket, 중앙 REST 제한기와 다음 비동기 태스크다.

```mermaid
flowchart TD
    SUP["Supervisor"] --> FEED["공유 KIS 시세 수신기"]
    FEED --> Q1["005930 이벤트 큐"]
    FEED --> Q2["000660 이벤트 큐"]
    FEED --> Q3["세 번째 종목 이벤트 큐"]
    Q1 --> W1["SymbolActor 1"]
    Q2 --> W2["SymbolActor 2"]
    Q3 --> W3["SymbolActor 3"]
    W1 --> CENTRAL["중앙 위험·주문 관리자"]
    W2 --> CENTRAL
    W3 --> CENTRAL
    CENTRAL --> KISORDER["KIS 주문 어댑터"]
```

- `SymbolActor`는 종목별 순차 상태머신이라 같은 종목의 틱 순서와 상태 변경을 보장한다.
- 중앙 주문 관리자는 모든 종목의 현금·동시 포지션·호출한도·멱등성을 단일 기준으로 판단한다.
- CPU가 무거운 백테스트·특징 계산만 별도 프로세스 풀로 보낸다.
- 블로킹 라이브러리는 제한된 스레드 풀로 격리한다.
- 태스크 실패는 Supervisor가 감지하고 재시작하되 DB 이벤트와 KIS 잔고로 상태를 복구한다.
- 한 종목 진입 워커를 멈춰도 다른 보유종목의 보호 워커와 중앙 위험 엔진은 계속 실행한다.

## 10. AI와 자동화 워커의 통신

AI와 워커는 채팅 메모리나 직접 함수 호출만으로 상태를 공유하지 않는다.

- 워커는 구조화 이벤트와 상태 스냅샷을 DB·Outbox에 저장한다.
- AI는 읽기 전용 시장 스냅샷을 검토하고 버전·만료가 있는 정책 결정을 기록한다.
- 오케스트레이터는 사용자 진입 위임, AI 정책, 정량 신호의 교집합만 주문 관리자에 전달한다.
- 명령에는 `command_id`, 사용자 ID, nonce, 만료시각, 대상 종목, 범위, 정책 버전을 둔다.
- 모든 소비자는 이벤트 ID와 명령 ID로 멱등 처리한다.
- 외부 AI나 대시보드가 끊겨도 기존 포지션 보호는 로컬에서 계속된다.
- Codex Plus와 OpenAI API는 인증·한도·과금이 다른 자원으로 계측한다.

GitHub Pages는 보고서와 분석 코멘트 화면만 맡는다. 권위 있는 원격 지시는 사용자가 Android Codex 앱의 현재 프로젝트 대화로 보낸다. PC 에이전트는 사용자 지시를 받은 뒤 대시보드 코멘트를 조회하고, 필요한 필드를 정규화해 로컬 FastAPI 제어 API에 기록한다.

```mermaid
flowchart LR
    PAGE["GitHub Pages 보고서·코멘트"] --> AGENT["PC Codex 에이전트가 조회"]
    USER["Android Codex 사용자 지시"] --> AGENT
    AGENT --> VALIDATE["명령 정규화·승인범위 검증"]
    VALIDATE --> LOCAL["로컬 FastAPI 제어 API"]
    LOCAL --> OUTBOX["DB·Outbox"]
    OUTBOX --> ORCHESTRATOR["운영 오케스트레이터"]
```

- 대시보드 코멘트만으로는 `WATCH_SELECT`나 `ENTRY_MANDATE`를 만들지 않는다.
- Android Codex 메시지의 사용자 지시가 있어야 에이전트가 작업을 시작한다.
- Codex 연결이 끊겨도 기존 포지션 보호는 로컬 위험 엔진이 계속한다.
- 코멘트 저장소는 후보·분석 메모만 보관하며 계좌·주문·비밀정보를 받지 않는다.
- 코멘트 저장 방식은 GitHub 기반 주석 저장소 또는 별도 최소 백엔드 중 Phase 2에서 결정한다.
# KOSPI 시장 전체 위험 데이터 경로

`MarketWideCollector`는 KIS REST에서 30초 간격으로 다음 자료를 수집한다.

- 국내업종 현재지수 `FHPUP02100000`: KOSPI 지수·등락률·고가·저가·거래대금·상승/보합/하락 종목 수
- 시장별 투자자매매동향 `FHPTJ04030000`: 개인·외국인·기관과 금융투자·보험·투신·사모펀드·은행·기타금융·연기금 등
- 프로그램매매 투자자매매동향 `HHPPG046600C1`: 차익·비차익·전체 프로그램 순매수

원본 단위는 KIS 응답의 거래대금 단위인 백만원으로 보존한다. `market_wide_snapshots` 테이블에는 핵심 조회 열과 전체 공개 가능 payload를 함께 저장한다. 최근 5분·15분 변화량은 누적 순매수의 차분으로 계산하며 절대 누적액과 분리한다.

```text
KIS REST -> MarketWideCollector -> MarketWideMonitor
                                  -> market_wide_snapshots
                                  -> MarketRegimeGuard -> TradingRuntimeCore
                                  -> 상태전환 SMTP
                                  -> 5분 지연 market-status.json -> GitHub Pages
```

GitHub 게시 실패, SMTP 실패, AI 중단은 시장 위험 판단과 주문 보호 경로에 영향을 주지 않는다. 공개 JSON 스키마는 `danta-market-status-v1`이며 계좌·보유수량·주문·키를 포함하지 않는다.

## 수급의 질과 연속성

장중 누적액과 별도로 KIS 시장별 투자자 일별동향을 최근 10거래일까지 `market_investor_daily`에 저장한다. 향후 시각화가 바로 가능하도록 다음 파생값도 공개 JSON에 포함한다.

- 외국인·연기금 등·투신 각각의 연속 순매수 일수
- 최근 5일·10일 중 외국인+연기금 등+투신이 모두 순매수한 일수
- 기관계 순매수의 대부분이 금융투자이고 연기금 등·투신은 순매도인 경고 패턴
- 가격 상승 동반 핵심수급, 하락 중 핵심수급 흡수, 가격 상승 중 핵심수급 이탈, 가격·핵심수급 동반 하락 구분

이 값은 수급의 성격을 설명하는 특성이지 상승 보장 신호가 아니다. 프로그램매매는 투자자 분류와 중복되는 거래 방식이므로 외국인·기관 순매수와 합산하지 않는다.
