#!/usr/bin/env python3
"""
backtest/reentry_portfolio.py — R2 재진입 규칙을 '실제 포트폴리오'에 검증
========================================================
reentry_sweep는 지수(KODEX200) 격리 결과였다. 여기서는 R2(20일 저점 +5% 반등 재진입)를
실제 4전략 모멘텀 바스켓(longrun)에 얹어 현행(R0 대칭-8%)과 나란히 비교한다.

가드는 상태머신(히스테리시스): 청산은 현행 고정(dd<=-8% 또는 VIX>30), 재진입만 규칙별.
⚠️ 데이터 창이 yf.py(2y) 한계라 최근 ~1.5년(강세장+2026 급락+반등)뿐 — 다국면 검증은
   reentry_sweep(10년 지수) 참조. 여기선 '실제 바스켓에서 R2가 손해 안 끼치나/이번 반등 잡나'.
"""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))
from longrun import _build_data, LongBacktest, benchmark  # noqa: E402
from signal_generator import DD_GUARD_THRESHOLD, DD_GUARD_LOOKBACK  # noqa: E402
from config import VIX_THRESHOLD  # noqa: E402
from baseline import BaselineMomentum126      # noqa: E402
from aggressive import ConcentratedMomentum   # noqa: E402
from vol_tilted import VolTiltedConcentrated   # noqa: E402


class StatefulGuard:
    """상태머신 가드. 청산=dd<=-8% 또는 VIX>30. 재진입=규칙별(R0 대칭 / R2 저점+5%)."""

    def __init__(self, prices, reentry="R0", th=DD_GUARD_THRESHOLD, lb=DD_GUARD_LOOKBACK):
        self.reentry = reentry
        kd = prices.kodex
        vix = prices.vix
        self.states = {}
        state = "BULL"
        for f in [d for d in kd.index if pd.Timestamp(d).weekday() == 4]:
            s = kd[kd.index <= f].dropna()
            if len(s) < lb:
                self.states[f] = "BULL"
                continue
            cur = float(s.iloc[-1]); win = s.tail(lb)
            high = float(win.max()); low = float(win.min()); dd = cur / high - 1
            vs = vix[vix.index <= f].dropna() if vix is not None else []
            vix_bear = len(vs) > 0 and float(vs.iloc[-1]) > VIX_THRESHOLD
            if state == "BULL":
                if dd <= th or vix_bear:
                    state = "BEAR"
            else:
                reok = (dd > th) if reentry == "R0" else (cur > low * 1.05)
                if reok and not vix_bear:
                    state = "BULL"
            self.states[f] = state
        self._fri = sorted(self.states)

    def market(self, date):
        cand = [f for f in self._fri if f <= date]
        return {"market_status": self.states[max(cand)] if cand else "BULL"}


def main():
    universes, prices = _build_data(80)
    strats = [BaselineMomentum126(), ConcentratedMomentum(), VolTiltedConcentrated()]
    fr = LongBacktest(universes, prices, StatefulGuard(prices, "R0"), strats)._fridays()
    bench = benchmark(prices, fr[0], fr[-1])

    print(f"\n🗓  검증 구간: {fr[0]:%Y-%m-%d} ~ {fr[-1]:%Y-%m-%d} ({len(fr)-1}주)")
    print("   (yf 2y 한계 = 강세장+급락+반등. 다국면은 reentry_sweep 10년 참조)\n")
    res = {}
    for rule in ["R0", "R2"]:
        res[rule] = LongBacktest(universes, prices, StatefulGuard(prices, rule), strats).run()

    print(f"{'전략':<28}{'R0 net':>9}{'R0 MDD':>8} │{'R2 net':>9}{'R2 MDD':>8}  {'Δnet':>7}")
    for name in res["R0"]:
        a, b = res["R0"][name], res["R2"][name]
        dn = b["total_net_pct"] - a["total_net_pct"]
        print(f"{name:<28}{a['total_net_pct']:>+8.1f}%{a['mdd_pct']:>+7.1f}% │"
              f"{b['total_net_pct']:>+8.1f}%{b['mdd_pct']:>+7.1f}%  {dn:>+6.1f}%p")
    print(f"{'[벤치] KODEX200 매수보유':<28}{bench['total_net_pct']:>+8.1f}%{bench['mdd_pct']:>+7.1f}%")
    # 지금 상태 요약
    g = StatefulGuard(prices, "R2")
    last = g._fri[-1]
    print(f"\n현재 R2 판정(최근 {last:%Y-%m-%d}): {g.states[last]}  "
          f"(R0 판정: {StatefulGuard(prices,'R0').states[last]})")
    print("판독: R2 net이 크게 안 나빠지면서 MDD 개선/유지 + 이번 반등 재진입이면 실포트에서도 유효.")


if __name__ == "__main__":
    main()
