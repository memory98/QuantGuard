# QuantGuard

국내 ETF 모멘텀 자동매매 시스템 + 전략 연구 + 미국 종이봇 + 웹 통제판.

> 상세 명세는 Notion **SPECIFICATION** / 이력은 **HISTORY(버전 히스토리 DB)** 참조.
> 코드 수정 절차·주간분석 규칙은 `CLAUDE.md`(로컬).

## 📁 구조 한눈에

| 경로 | 무엇 | 실전? |
|---|---|---|
| `rambdaA/` | **시그널 엔진**(Lambda A) — 모멘텀 랭킹·유니버스 스크리닝·VIX+DD 가드·유니버스 스냅샷 | 💰 실전 |
| `rambdaB/` | **주문 엔진**(Lambda B) — 잔고조회·리밸런싱·매도/매수 집행 | 💰 실전 |
| `strategies/` | 섀도우 전략 정의(baseline·집중·속도·레버리지·vol_tilted 등) | 종이 |
| `backtest/` | 백테스트·가드실험·**섀도우 전진검증**·미국돌파 — 인덱스는 `backtest/README.md` | 종이 |
| `scripts/` | 주간 성과 분석(`analyze_returns.py`) + 실행 매뉴얼 `README.md` | 분석 |
| `paper_trader/` | 미국주식 **종이 자동매매 봇**(실돈 0) | 종이 |
| `tests/` | 실행경로 단위테스트(fix14/17/18 회귀 방지). CI 배포 게이트 | — |
| `dashboard/` | 잔고 대시보드 + `.venv`(파이썬 실행환경) | — |
| `../quant-web/` | 종이봇 **웹 통제판**(Flask, localhost). 별도 git 레포 | — |

## 🕒 무엇이 언제 도나
- **매주 월 15:05/15:15 KST** — Lambda A/B 정기 리밸런싱(EventBridge). ⚠️ 14:00/14:20 권장.
- **매주 월 16:00 KST** — GitHub Actions가 섀도우 전진검증 실행 → `shadow_ledger.json` 커밋 + 텔레그램.
- **rambdaA/B push 시** — CI가 `tests/` 통과해야만 배포(deploy.yml). 실행버그 배포 차단.

## ▶ 자주 쓰는 실행
```bash
PY=dashboard/.venv/bin/python
$PY -m unittest discover -s tests           # 실행경로 테스트
$PY scripts/analyze_returns.py --exclude-dates "..." --net-deposits '{}'  # 주간분석
$PY backtest/shadow_forward.py              # 섀도우 성적표
$PY backtest/longrun.py --top 80            # 장기 백테스트
$PY paper_trader/us_paper_bot.py            # 미국 종이봇(장마감 자동정리)
# 웹 통제판: ../stock/dashboard/.venv/bin/python ../quant-web/app.py → localhost:8787
```

## ⚖️ 핵심 원칙 (하드-원 교훈)
1. **신호/가드 파라미터 튜닝 동결** — 임계값·R2·콤보 3연속 검증 탈락. 백테스트 승자는 실포트/OOS에서 무너짐.
2. **값어치는 청산 가드의 낙폭 방어**, 재진입/신호 튜닝 아님.
3. **진짜 판정은 섀도우 OOS 누적**(2026-08-03~), 백테스트 아님.
4. **실돈 지키는 건 신뢰성**(테스트·데이터 가드·CI 게이트) — 신호가 아님.
5. 외부 API mock은 **공식 필드/독립 사용처 근거**로(코드에서 안 베낌). `data/`·`log/`는 커밋 금지.
