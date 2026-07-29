#!/usr/bin/env python3
"""
backtest/us_breakout.py — 미국주식 추세돌파 전략 백테스트 (개념 검증 + 파라미터 스윕)
========================================================
개별 종목: N일 신고가 돌파 + M일 이평 위 진입 / 트레일링 스탑·손절 청산 / 최대 K종목.
--sweep 로 파라미터 조합을 훑어 '개선 여지가 진짜 있는지(아니면 다 SPY 근처인지)' 본다.

⚠️ 한계(정직히): 유니버스가 '오늘의' 대형주 → 강한 생존편향(절대치 과대). 일봉 근사.
   진입=신호 다음날 종가. '규칙이 되나/개선되나' 확인용이며 채택은 종이 OOS 후.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
from guard_sweep import fetch  # noqa: E402  미국 심볼도 됨(.KS 안 붙임)

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "ADBE", "CRM", "QCOM", "TXN", "INTC", "JPM", "V", "MA", "UNH", "JNJ",
    "XOM", "CVX", "WMT", "PG", "HD", "COST", "LLY", "ABBV", "KO", "PEP",
    "CAT", "BA", "GE", "DIS", "NKE",
]
BENCH = "SPY"


class USBreakoutBacktest:
    def __init__(self, prices, dates, high_window=20, ma_window=50,
                 trail=0.08, stop=0.05, max_pos=5, cost=0.0005):
        self.p = prices
        self.dates = dates
        self.hw = high_window
        self.mw = ma_window
        self.trail = trail
        self.stop = stop
        self.max_pos = max_pos
        self.cost = cost

    def _signal(self, sym, i):
        s = self.p[sym]
        if i < self.mw:
            return False
        window = s.iloc[i - self.hw + 1:i + 1]
        if len(window) < self.hw:
            return False
        c = s.iloc[i]
        ma = s.iloc[i - self.mw + 1:i + 1].mean()
        return c >= window.max() and c > ma

    def run(self):
        cash = 1.0
        pos = {}
        curve = []
        trades = []
        for i in range(len(self.dates)):
            for sym in list(pos.keys()):
                s = self.p[sym]
                c = float(s.iloc[i])
                prev = float(s.iloc[i - 1]) if i > 0 else c
                pos[sym]["value"] *= (c / prev) if prev > 0 else 1.0
                pos[sym]["peak"] = max(pos[sym]["peak"], c)
                if c <= pos[sym]["peak"] * (1 - self.trail) or c <= pos[sym]["entry"] * (1 - self.stop):
                    cash += pos[sym]["value"] * (1 - self.cost)
                    trades.append(c / pos[sym]["entry"] - 1)
                    del pos[sym]
            equity = cash + sum(v["value"] for v in pos.values())
            for sym in WATCHLIST:
                if len(pos) >= self.max_pos:
                    break
                if sym in pos or sym not in self.p:
                    continue
                if i + 1 < len(self.dates) and self._signal(sym, i):
                    size = min(equity / self.max_pos, cash)
                    if size <= 0:
                        continue
                    entry_px = float(self.p[sym].iloc[i + 1])
                    cash -= size * (1 + self.cost)
                    pos[sym] = {"entry": entry_px, "peak": entry_px, "value": size}
            curve.append(cash + sum(v["value"] for v in pos.values()))
        return pd.Series(curve, index=self.dates), trades


def stats(curve, trades):
    mdd = float((curve / curve.cummax() - 1).min())
    days = max((curve.index[-1] - curve.index[0]).days, 1)
    cagr = float(curve.iloc[-1]) ** (365 / days) - 1
    wr = (sum(1 for t in trades if t > 0) / len(trades) * 100) if trades else 0
    return (curve.iloc[-1] - 1) * 100, cagr * 100, mdd * 100, len(trades), wr


def load_prices():
    prices = {}
    for sym in WATCHLIST + [BENCH]:
        try:
            prices[sym] = fetch(sym, "10y")
        except Exception:
            pass
    cal = prices[BENCH].index
    for sym in WATCHLIST:
        if sym in prices:
            prices[sym] = prices[sym].reindex(cal).ffill()
    return prices, cal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    print("📡 미국주식 시세 수집(range=10y)...")
    prices, cal = load_prices()
    puni = {s: prices[s] for s in WATCHLIST if s in prices}
    bench = prices[BENCH] / prices[BENCH].iloc[0]
    bt_b, bc, bm, *_ = stats(bench, [])
    print(f"   기간 {cal[0].date()} ~ {cal[-1].date()} ({len(cal)}일)")
    print(f"\n{'설정':<28}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'거래':>7}{'승률':>7}")

    if args.sweep:
        configs = [
            ("기본 hw20/ma50 tr8 st5", dict(high_window=20, ma_window=50, trail=0.08, stop=0.05)),
            ("장기돌파 hw50/ma100", dict(high_window=50, ma_window=100, trail=0.08, stop=0.05)),
            ("단기돌파 hw10/ma20", dict(high_window=10, ma_window=20, trail=0.08, stop=0.05)),
            ("넓은트레일 tr12", dict(high_window=20, ma_window=50, trail=0.12, stop=0.06)),
            ("좁은트레일 tr5", dict(high_window=20, ma_window=50, trail=0.05, stop=0.04)),
            ("집중 max3", dict(high_window=20, ma_window=50, trail=0.08, stop=0.05, max_pos=3)),
            ("분산 max10", dict(high_window=20, ma_window=50, trail=0.08, stop=0.05, max_pos=10)),
        ]
        for label, kw in configs:
            c, t = USBreakoutBacktest(puni, cal, **kw).run()
            tot, cagr, mdd, n, wr = stats(c, t)
            print(f"{label:<28}{tot:>+8.0f}%{cagr:>+7.1f}%{mdd:>+7.1f}%{n:>7}{wr:>6.0f}%")
    else:
        c, t = USBreakoutBacktest(puni, cal).run()
        tot, cagr, mdd, n, wr = stats(c, t)
        print(f"{'US 돌파(기본)':<28}{tot:>+8.0f}%{cagr:>+7.1f}%{mdd:>+7.1f}%{n:>7}{wr:>6.0f}%")

    print(f"{'SPY 매수보유':<28}{bt_b:>+8.0f}%{bc:>+7.1f}%{bm:>+7.1f}%{'—':>7}{'—':>7}")
    print("\n⚠️ 생존편향+일봉근사 → 절대치 과대. 여러 설정이 다 SPY 근처면 '규칙에 엣지 없음'.")


if __name__ == "__main__":
    main()
