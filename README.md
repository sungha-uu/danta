# Danta

KOSPI 단기매매 후보를 매일 선별하고 사용자 승인 범위에서 모의 주문·자동 손절·적응형 수익관리를 검증하는 자동화 프로젝트입니다.

## 현재 상태

- 활성 증권사: 한국투자증권 Open API(KIS)
- 기본 환경: 모의투자
- KRX 실제 데이터 후보 30개 산출과 KIS 현재가 교차검증 구현
- 기존 일봉 종가 `box-quant-v1`은 연구용·주문 불가, 실제 1분봉 백필·60분봉 집계 기반 `box-elasticity-v3-60m-target10` 연구판 구현
- GitHub Pages 실제 정적 대시보드·SMTP 배포 알림 구현
- KIS 자격증명·현재가·일봉·잔고·주문가능현금·WebSocket 접속키 진단 구현
- KIS 현금주문 계약 구현, 주문 제출 기본 잠금
- 실전 주문: 잠금

모든 개발 에이전트는 작업 전에 [`DOCS/agent.md`](DOCS/agent.md)를 읽고, `-7%` 강제 손절과 사용자 승인 정책을 우선합니다.

## 로컬 검증

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\danta.exe dashboard --demo --output dashboard\dist
.\.venv\Scripts\danta.exe daily-report
.\.venv\Scripts\danta.exe intraday-report
```

비밀정보와 계좌정보는 `.secrets/`에만 저장하며 Git과 GitHub Pages에 게시하지 않습니다.

일일 리포트: https://sungha-uu.github.io/danta_report/

현재 공개된 일봉 기준선은 데이터 연결·화면 검증용이며 자동매수 승인에 사용할 수 없습니다.
