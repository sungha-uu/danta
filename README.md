# Danta

KOSPI 단기매매 후보를 매일 선별하고 사용자 승인 범위에서 모의 주문·자동 손절·적응형 수익관리를 검증하는 자동화 프로젝트입니다.

## 현재 상태

- 활성 증권사: 한국투자증권 Open API(KIS)
- 기본 환경: 모의투자
- 정적 후보 30개 데모 대시보드 구현
- KIS 자격증명·현재가·잔고·WebSocket 접속키 진단 구현
- 실전 주문: 잠금

모든 개발 에이전트는 작업 전에 [`DOCS/agent.md`](DOCS/agent.md)를 읽고, `-7%` 강제 손절과 사용자 승인 정책을 우선합니다.

## 로컬 검증

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\danta.exe dashboard --demo --output dashboard\dist
```

비밀정보와 계좌정보는 `.secrets/`에만 저장하며 Git과 GitHub Pages에 게시하지 않습니다.
