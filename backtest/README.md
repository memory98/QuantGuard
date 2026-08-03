# backtest/ — 연구 모듈 인덱스

> ⚠️ 전부 **로컬 연구용**. 실전 Lambda(rambdaA/rambdaB)와 무관, 실주문 없음.
> 대부분의 결론은 **"쉬운 알파 없음, 값어치는 청산 가드(낙폭)뿐"**. 튜닝 동결 상태([[signal-tuning-freeze]]).
> 실행: `dashboard/.venv/bin/python backtest/<파일>.py`

## 상태 범례
🟢 활성(계속 쓰임) · 🟡 참고(결론 남았음) · ⚪ 도구(온디맨드)

| 파일 | 무엇 | 결론/상태 |
|---|---|---|
| `shadow_forward.py` | **섀도우 전진검증 파이프라인** (매주 CI 실행) | 🟢 유일한 진짜 OOS. 2026-08-03~ 실유니버스 누적 |
| `runner.py` | 시그널 아카이브 replay(초기) | 🟡 longrun으로 대체됨 |
| `longrun.py` | 장기 백테스트(멀티유니버스·전략별 룩백·가드) | 🟢 전략 비교 주력 |
| `guard_sweep.py` | DD임계값 10년 다구간 스윕 | 🟡 -8% 유지 결론(-10%는 과최적화) |
| `guard_mechanisms.py` | 가드 방식(SMA/TSMOM/DualMA/콤보) 비교 | 🟡 콤보 지수선 최선이나 포트 미검증 |
| `validate_combo.py` | 콤보 가드 실포트 검증 | 🟡 포트선 이점 미확인 |
| `reentry_sweep.py` | 재진입 규칙 10년 지수 비교 | 🟡 R2 지수선 우세 |
| `reentry_portfolio.py` | R2 재진입 실포트 검증 | 🟡 **R2 포트선 탈락**(수익만↓) |
| `us_breakout.py` | 미국 추세돌파 백테스트(+스윕) | 🟡 SPY 수준, 엣지 없음 |

## 핵심 교훈(반복 확인됨)
1. **어떤 전략도 매수보유를 확실히 못 이김.** 절대수익은 시장/생존편향.
2. **백테스트 승자는 실포트/OOS에서 무너진다** (임계값·R2·콤보 전부).
3. **값어치 = 청산 가드의 낙폭 방어.** 재진입/신호 튜닝은 값어치 안 나옴.
4. 진짜 판정은 백테스트가 아니라 **섀도우 OOS 누적**(진행 중).

## strategies/ (전략 정의)
`baseline`(현행 재현)·`aggressive`(집중)·`fast`(63일)·`leverage`(2X)·`vol_adjusted`(방어형,폐기)·`vol_tilted`(리스크조정+집중). 카탈로그·성과는 Notion "섀도우 전략 연구소".
