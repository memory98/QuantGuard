#!/usr/bin/env python3
"""
backtest/reentry_sweep.py — 재진입 규칙 다구간(10년) 비교
========================================================
청산(exit)은 현행 고정(20일 고점 대비 -8% 또는 VIX>30). **재진입(re-entry) 규칙만** 바꿔
KODEX200 격리(보유/현금 스위칭)로 10년·연도별 비교. "빠른 재진입"이 진짜 개선인지
(반등 포착 vs 휘프소 증가) 데이터로 판정.

재진입 규칙:
  R0 대칭(-8%)   : dd가 -8% 위로 회복 (현행)
  R1 완화(-12%)  : dd가 -12% 위로 회복하면 조기 재진입
  R2 저점반등+5% : 20일 저점 대비 +5% 오르면
  R3 MA20 상향   : 종가가 20일 이동평균 위로
  R4 MA10 상향   : 종가가 10일 이동평균 위로 (가장 빠름)
"""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "rambdaA"))
from guard_sweep import fetch  # noqa: E402
from signal_generator import DD_GUARD_TICKER, DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD  # noqa: E402
from config import VIX_THRESHOLD  # noqa: E402

LB = DD_GUARD_LOOKBACK  # 20


def indicators(idx, date):
    s = idx[idx.index <= date].dropna()
    cur = float(s.iloc[-1])
    win = s.tail(LB)
    return cur, float(win.max()), float(win.min()), float(s.tail(20).mean()), float(s.tail(10).mean())


def reentry_ok(name, cur, high, low, ma20, ma10):
    dd = cur / high - 1
    if name == "R0 대칭-8%":      return dd > -0.08
    if name == "R1 완화-12%":     return dd > -0.12
    if name == "R2 저점반등+5%":  return cur > low * 1.05
    if name == "R3 MA20상향":     return cur > ma20
    if name == "R4 MA10상향":     return cur > ma10
    return dd > -0.08


def run(idx, vix, rule, cost=0.001):
    fri = [d for d in idx.index if pd.Timestamp(d).weekday() == 4]
    fri = [d for d in fri if d >= idx.index[LB + 1]]
    state, prev_pos = "BULL", 1
    rets, dates, switches = [], [], 0
    ix = idx.index
    for i in range(len(fri) - 1):
        f0, f1 = fri[i], fri[i + 1]
        cur, high, low, ma20, ma10 = indicators(idx, f0)
        dd = cur / high - 1
        v = vix[vix.index <= f0].dropna()
        vx = float(v.iloc[-1]) if len(v) else None
        vix_bear = (vx is not None and vx > VIX_THRESHOLD)   # VIX 안전 오버라이드
        if state == "BULL":
            # 청산은 현행 고정: 20일 고점 대비 -8% 또는 VIX>30
            if (dd <= DD_GUARD_THRESHOLD) or vix_bear:
                state = "BEAR"
        else:
            # 재진입: 규칙별 조건 (VIX가 여전히 위험이면 보류)
            if reentry_ok(rule, cur, high, low, ma20, ma10) and not vix_bear:
                state = "BULL"
        pos = 1 if state == "BULL" else 0
        en, ex = ix[ix > f0], ix[ix > f1]
        if len(en) == 0 or len(ex) == 0:
            continue
        r = pos * float(idx.loc[ex[0]] / idx.loc[en[0]] - 1)
        if pos != prev_pos:
            r -= cost
            switches += 1
        rets.append(r); dates.append(ex[0]); prev_pos = pos
    return pd.Series(rets, index=pd.to_datetime(dates)), switches


def stats(w, sw):
    eq = (1 + w).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((w.index[-1] - w.index[0]).days, 1)
    cagr = float(eq.iloc[-1]) ** (365 / days) - 1
    inv = float((w != 0).mean()) * 100
    return (eq.iloc[-1] - 1) * 100, cagr * 100, mdd * 100, inv, sw


def yearly_ret(w):
    return {yr: (float((1 + g).prod()) - 1) * 100 for yr, g in w.groupby(w.index.year)}


def main():
    print("📡 KODEX200 + VIX 10년 수집...")
    idx = fetch(f"{DD_GUARD_TICKER}.KS", "10y")
    vix = fetch("%5EVIX", "10y")
    rules = ["R0 대칭-8%", "R1 완화-12%", "R2 저점반등+5%", "R3 MA20상향", "R4 MA10상향"]
    print(f"   {idx.index[0].date()} ~ {idx.index[-1].date()}\n")
    print(f"{'재진입규칙':<14}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'투자%':>7}{'스위칭':>7}")
    per = {}
    for r in rules:
        w, sw = run(idx, vix, r)
        per[r] = yearly_ret(w)
        tot, cagr, mdd, inv, s = stats(w, sw)
        print(f"{r:<14}{tot:>+8.0f}%{cagr:>+7.1f}%{mdd:>+7.1f}%{inv:>6.0f}%{s:>7}")
    # 하락→반등 해 강조(2020 코로나 반등, 2022 약세 후)
    yrs = sorted(next(iter(per.values())).keys())
    print(f"\n📅 연도별 수익률 (반등 포착이 중요한 해 주목: 2020, 2023)")
    print("규칙".ljust(14) + "".join(f"{y:>7}" for y in yrs))
    for r in rules:
        print(r.ljust(14) + "".join(f"{per[r].get(y,0):>6.0f}%" for y in yrs))
    print("\n판독: 반등해 수익↑ + MDD 안 나빠지고 + 스위칭 과하지 않은 규칙이 진짜 개선. "
          "스위칭 급증/MDD 악화면 휘프소 = 개악.")


if __name__ == "__main__":
    main()
