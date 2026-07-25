# 한국투자증권·키움증권 API 비교와 결정

- 문서 등급: 증권사 선택 기록
- 활성 주문 공급자: 한국투자증권 Open API(KIS)
- 대안 공급자: 키움증권 REST API
- 결정일: 2026-07-25

## 1. 결정

사용자 승인에 따라 한국투자증권 Open API를 1순위로 구현한다. 키움증권은 장애 대비나 향후 전환을 위한 어댑터 후보일 뿐이며, 별도 사용자 승인 전에는 키움 계좌·키·주문 기능을 요구하거나 구현하지 않는다.

KIS를 선택한 이유:

- 국내주식 시세·잔고·현금 주문·정정/취소·WebSocket을 모두 제공한다.
- 실전투자와 모의투자 자격증명을 분리해 단계적으로 검증할 수 있다.
- 공식 GitHub에 Python·LLM용 기능별 예제, WebSocket 예제, 전략 빌더, 백테스터가 있다.
- 공식 AI 에이전트 확장 자료가 있어 에이전트 주도 개발에 유리하다.
- BanKIS 일반 온라인 수수료 표면값이 키움 일반 온라인 수수료보다 약간 낮다.
- 공식 샘플의 필수 설정에는 키움식 공인 IP 등록이 포함되어 있지 않다.

## 2. 비교

| 항목 | 한국투자증권 Open API | 키움증권 REST API | 프로젝트 판단 |
| --- | --- | --- | --- |
| 우선순위 | 활성 1순위 | 비활성 대안 | KIS 구현 |
| 자동 매수·매도 | 가능 | 가능 | 차이 없음 |
| 모의·실전 | 별도 키로 지원 | 별도 키로 지원 | 차이 없음 |
| 실시간 | WebSocket 체결·호가·체결통보 | WebSocket 체결·호가 | 모두 가능 |
| 공식 개발 자료 | Python, LLM 예제, 전략 빌더, 백테스터, AI 확장 | 포털 가이드·샘플 | KIS 우위 |
| 자산군 확장 | 국내외 주식·ETF/ETN·선물옵션 등 공식 샘플 | 국내주식 중심으로 시작하기 쉬움 | 장기적으로 KIS 유리 |
| 공인 IP | 공식 샘플 준비항목에 별도 등록 없음 | 공식 안내상 IP 등록 | 노트북 개발은 KIS가 단순 |
| 호출한도 | 계정·환경별 최신 조건을 진단, 모의 제한 낮음 | 공식 표기 한도가 비교적 명확 | KIS는 진단 필수 |
| 구현 위험 | TR ID·연속조회·실전/모의 차이 관리 필요 | IP·세션·호출한도 관리 필요 | 둘 다 어댑터 필요 |

## 3. 수수료

2026-07-25 확인 가능한 일반 온라인 안내:

| 증권사 | KRX 국내주식 온라인 | NXT 국내주식 온라인 |
| --- | ---: | ---: |
| 한국투자증권 BanKIS | 0.0140527% | 0.0130527% |
| 키움증권 일반 온라인 | 0.015% | 0.0145% |

실제 계좌에는 신규고객·이벤트·협의 수수료가 적용될 수 있으므로 계좌 개설 후 앱의 `수수료율 조회`에서 최종 확인한다. 주문금액 1,000만 원에서 0.001%p 차이는 편도 약 100원이므로 체결 슬리피지, 미체결, 장애 복구, -7% 손절 신뢰성이 더 중요하다. 매도 시 세금과 거래소 관련 비용도 별도 반영한다.

## 4. 구현 원칙

공통 브로커 포트:

- `BrokerAuthProvider`
- `MarketDataProvider`
- `RealtimeQuoteProvider`
- `OrderExecutionProvider`
- `AccountProvider`
- `ProviderDoctor`

어댑터:

- `KisBrokerAdapter`: 1차 구현 대상
- `KiwoomBrokerAdapter`: 구현하지 않는 대안 인터페이스

도메인 규칙은 증권사와 무관하게 유지한다.

- 사용자 승인 없는 매수 금지
- 평균 체결가 대비 -7% 전량 시장가 손절
- 물타기 금지
- 주문 멱등성·미체결·체결·잔고 대조
- 외부 LLM과 GitHub Pages를 실시간 주문 경로에서 분리

## 5. 재검토 조건

다음 상황에서만 키움 어댑터 추가 또는 증권사 재선정을 검토한다.

- KIS 주문·WebSocket 안정성이 모의·소액 실전에서 요구 수준에 미달
- 실제 적용 수수료가 키움보다 의미 있게 불리함
- KIS 호출·구독 한도가 전략에 부족함
- 브로커 장애 대비 이중화가 필요함

재검토 전까지 개발 문서와 코드 예제의 기본 공급자는 항상 KIS다.

## 6. 공식 자료

- [KIS Developers](https://apiportal.koreainvestment.com/)
- [한국투자증권 공식 Open API GitHub](https://github.com/koreainvestment/open-trading-api)
- [한국투자증권 공식 AI 확장 저장소](https://github.com/koreainvestment/kis-ai-extensions)
- [한국투자증권 수수료 안내](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp)
- [키움 REST API 포털](https://openapi.kiwoom.com/)
- [키움 수수료 안내](https://www.kiwoom.com/h/help/fee/VHelpStockFeeView)
