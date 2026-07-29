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

## 다음 할 일 (순서대로, 하나씩)
- [ ] **STEP 1. 봇 리팩터**: `us_paper_bot.py`의 `Bot.run()`을 스레드에서 시작/중지 가능하게(중지 플래그·start()/stop() 메서드). CLI 실행은 그대로 유지.
- [ ] **STEP 2. Flask 서버 골격**: `paper_trader/web/app.py` — 라우트 `/`(대시보드), `/api/status`(JSON), `/api/start`, `/api/stop`. `dashboard/.venv`로 실행.
- [ ] **STEP 3. 상태 API**: state.json+trades.jsonl+daily.jsonl 읽어 {실행중여부, 평가액(원), 현금, 보유종목, 최근거래, 당일·누적수익, vsSPY} 반환.
- [ ] **STEP 4. 대시보드 HTML**: 실행상태·평가액·보유·최근거래·일별 P&L 표시, 수초마다 자동새로고침. Start/Stop 버튼(→ /api).
- [ ] **STEP 5. 설정 편집(선택)**: 웹에서 trail/stop/max_pos 등 config.json 수정("개선" 워크플로우 웹化).
- [ ] **STEP 6. 실행 안내**: 서버 켜는 법 + 브라우저 localhost:PORT 접속. 봇 시작/종료를 버튼으로.

## 주의/원칙
- 백테스트 숫자 짜맞추기(과최적화) 금지 — 개선도 OOS로 판정.
- 웹서버는 localhost 바인딩만(0.0.0.0 금지), 인증 없는 외부 노출 금지.
- 봇 스레드가 죽어도 장마감 자동청산·데이터로그는 유지되게.

## 이어하기 방법
새 대화에서: "웹 통제 이어서 만들어줘" → 이 문서의 첫 `[ ]` 미완 단계부터. 완료 시 `[x]`로 바꾸고 커밋.
