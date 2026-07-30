# 미국 종이 봇 — 웹 통제(Web Control) 작업 계획 · 이어하기 문서

> 목적: 터미널 대신 **웹 UI로 봇을 켜고/끄고/상태 확인**. 세션이 끊겨도 이 문서로 이어간다.
> 새 세션이면: 이 문서 + 메모리 [[us-paper-bot]] 읽고 "다음 할 일"의 첫 미완 단계부터 진행.

## 지금까지 만들어진 것 (기반)
- `paper_trader/us_paper_bot.py` — 종이 봇(작동 확인). 규칙: 20일돌파+50MA / 트레일-10%·손절-5% / 최대5종목. 자본 1000만원(환율 환산). 미국장 시간인식 + 장마감 자동청산. 종료: Ctrl-C.
- `paper_trader/config.json` — 튜너블 파라미터.
- 데이터(로컬, git제외): `data/paper_us_state.json`, `data/paper_us_trades.jsonl`, `data/paper_us_daily.jsonl`, `log/paper_us.log`.
- 실행: `dashboard/.venv/bin/python paper_trader/us_paper_bot.py`
- ⚠️ 종이 전용(돈 0). 백테스트상 SPY 수준(엣지 미검증) — 학습·데이터축적용.

## 설계 방향 (결정됨)
- **로컬 Flask 웹서버**(localhost 전용, 외부 노출 X). 가벼움 + `dashboard/.venv`에 Flask 있는지 확인 후 없으면 설치.
- 봇을 **백그라운드 스레드**로 돌리고 웹에서 start/stop. 봇 로직은 재사용(us_paper_bot.py의 Bot/Portfolio).
- 웹은 데이터 파일을 읽어 상태 표시(봇과 파일로 느슨히 결합 → 단순·안전).
- 실계좌/실주문 연결 절대 없음(종이 유지).

## 진행 상황 (2026-07-30 대부분 완료)
설계 변경: 봇 스레드화 대신 **서브프로세스 제어**(웹이 봇을 Popen 실행, SIGINT로 안전종료) — 봇 CLI 그대로, 크로스폴더 OK. 웹앱은 별도 폴더 `../quant-web/app.py`(stock 옆).
- [x] **STEP 1. 봇 훅**: `us_paper_bot.py`에 `write_live()` 추가 → `data/paper_us_live.json`(현재 평가액·보유·수익). 스레드화 불필요(서브프로세스).
- [x] **STEP 2. Flask 서버**: `quant-web/app.py` — `/`, `/api/status`, `/api/start`, `/api/stop`(?force=1 강제). 127.0.0.1:8787.
- [x] **STEP 3. 상태 API**: live.json + trades.jsonl + daily.jsonl + 로그tail 반환. 검증 완료(start→alive→stop→정지).
- [x] **STEP 4. 대시보드 HTML**: 실행뱃지·평가액·누적%·현금·환율·보유표·최근거래·일별(vsSPY)·로그, 5초 자동새로고침, 시작/안전종료/강제 버튼.
- [ ] **STEP 5. 설정 편집(선택, 미완)**: 웹에서 config.json(trail/stop/max_pos 등) 수정 UI. ← **다음 이어할 지점**
- [x] **STEP 6. 실행 안내**: `stock/dashboard/.venv/bin/python quant-web/app.py` → http://127.0.0.1:8787

## 주의/원칙
- 백테스트 숫자 짜맞추기(과최적화) 금지 — 개선도 OOS로 판정.
- 웹서버는 localhost 바인딩만(0.0.0.0 금지), 인증 없는 외부 노출 금지.
- 봇 스레드가 죽어도 장마감 자동청산·데이터로그는 유지되게.

## 이어하기 방법
새 대화에서: "웹 통제 이어서 만들어줘" → 이 문서의 첫 `[ ]` 미완 단계부터. 완료 시 `[x]`로 바꾸고 커밋.
