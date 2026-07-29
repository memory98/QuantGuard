#!/usr/bin/env python3
"""
backtest/guard_mechanisms.py — 하락장 가드 '메커니즘' 대안 비교
========================================================
DD가드가 장기적으로 약한 헤지임이 드러났다(guard_sweep). 여기서는 임계값이 아니라
**판별 방식 자체**를 여러 개 만들어 똑같이 격리(KODEX200 보유/현금)·10년·연도별로 비교한다.

비교 규칙(모두 단독, VIX 오버레이 제외 — 메커니즘 자체를 격리):
  - DD-8% (현행 핵심): 20일 고점 대비 -8% 이하
  - SMA120 / SMA200: 종가가 N일 단순이동평균 아래면 BEAR (추세 이탈)
  - TSMOM-3m / 6m: 지수의 N개월 수익률이 음수면 BEAR (시계열 모멘텀)
  - DualMA-50/200: 단기 이평 < 장기 이평이면 BEAR (데드크로스)

판정 기준: 하락국면(2018/2020/2022) MDD를 꾸준히 줄이면서 상승국면 수익을 덜 깎고,
          휘프소(잦은 스위칭)가 과하지 않은 규칙이 robust.

production yf.py 미변경, 롱히스토리 페처(guard_sweep.fetch) 재사용.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "rambdaA"))

from guard_sweep import fetch  # noqa: E402  — 롱히스토리 페처 재사용
from signal_generator import DD_GUARD_TICKER  # noqa: E402


# ── 규칙(메커니즘) 정의 ──────────────────────────────────────
class Rule:
    name = "base"

    def status(self, idx: pd.Series, date) -> str:
        raise NotImplementedError


class DDGuard(Rule):
    def __init__(self, th=-0.08, lb=20):
        self.th, self.lb = th, lb
        self.name = f"DD{int(th*100)}%/{lb}d"

    def status(self, idx, date):
        s = idx[idx.index <= date].dropna()
        if len(s) < self.lb:
            return "BULL"
        dd = float(s.iloc[-1] / s.tail(self.lb).max() - 1)
        return "BEAR" if dd <= self.th else "BULL"


class SMAFilter(Rule):
    def __init__(self, window):
        self.w = window
        self.name = f"SMA{window}"

    def status(self, idx, date):
        s = idx[idx.index <= date].dropna()
        if len(s) < self.w:
            return "BULL"
        return "BEAR" if float(s.iloc[-1]) < float(s.tail(self.w).mean()) else "BULL"


class TSMom(Rule):
    def __init__(self, days, label):
        self.days = days
        self.name = f"TSMOM-{label}"

    def status(self, idx, date):
        s = idx[idx.index <= date].dropna()
        if len(s) <= self.days:
            return "BULL"
        return "BEAR" if float(s.iloc[-1] / s.iloc[-1 - self.days] - 1) < 0 else "BULL"


class DualMA(Rule):
    def __init__(self, short, long):
        self.s, self.l = short, long
        self.name = f"DualMA{short}/{long}"

    def status(self, idx, date):
        s = idx[idx.index <= date].dropna()
        if len(s) < self.l:
            return "BULL"
        return "BEAR" if float(s.tail(self.s).mean()) < float(s.tail(self.l).mean()) else "BULL"


class Combo(Rule):
    """여러 규칙의 OR 결합 — 하나라도 BEAR면 BEAR (느린 하락+빠른 급락 동시 커버 목적)."""

    def __init__(self, *subrules, label):
        self.subs = subrules
        self.name = label

    def status(self, idx, date):
        return "BEAR" if any(r.status(idx, date) == "BEAR" for r in self.subs) else "BULL"


# ── 엔진: 규칙을 받아 주간 보유/현금 스위칭 ─────────────────────
def weekly_returns(idx: pd.Series, rule: Rule, cost_per_switch=0.001):
    ix = idx.index
    fri = [d for d in ix if pd.Timestamp(d).weekday() == 4]
    fri = [d for d in fri if d >= ix[210]]  # 최장 룩백(200) 워밍업 후 시작
    out_r, out_d = [], []
    prev_pos, switches = 0, 0
    for i in range(len(fri) - 1):
        f0, f1 = fri[i], fri[i + 1]
        entry = ix[ix > f0]
        exit_ = ix[ix > f1]
        if len(entry) == 0 or len(exit_) == 0:
            continue
        d_en, d_ex = entry[0], exit_[0]
        pos = 0 if rule.status(idx, f0) == "BEAR" else 1
        wr = pos * float(idx.loc[d_ex] / idx.loc[d_en] - 1)
        if pos != prev_pos:
            wr -= cost_per_switch
            switches += 1
        out_r.append(wr)
        out_d.append(d_ex)
        prev_pos = pos
    return pd.Series(out_r, index=pd.to_datetime(out_d)), switches


def metrics(weekly: pd.Series, switches: int) -> dict:
    eq = (1 + weekly).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    days = max((weekly.index[-1] - weekly.index[0]).days, 1)
    cagr = float(eq.iloc[-1]) ** (365 / days) - 1
    return {"total": (float(eq.iloc[-1]) - 1) * 100, "cagr": cagr * 100,
            "mdd": mdd * 100, "invested": float((weekly != 0).mean()) * 100,
            "switches": switches}


def yearly_mdd(weekly: pd.Series) -> dict:
    out = {}
    for yr, g in weekly.groupby(weekly.index.year):
        eq = (1 + g).cumprod()
        out[yr] = float((eq / eq.cummax() - 1).min()) * 100
    return out


def main():
    ap = argparse.ArgumentParser(description="하락장 가드 메커니즘 비교")
    ap.add_argument("--range", type=str, default="10y")
    args = ap.parse_args()

    print(f"📡 롱히스토리 수집 (KODEX200, range={args.range})...")
    idx = fetch(f"{DD_GUARD_TICKER}.KS", args.range)
    print(f"   {len(idx)}일  {idx.index[0].date()} ~ {idx.index[-1].date()}")

    rules = [
        DDGuard(-0.08, 20),
        SMAFilter(120),
        TSMom(63, "3m"),
        DualMA(50, 200),
        Combo(SMAFilter(120), DDGuard(-0.08, 20), label="SMA120+DD8"),
        Combo(SMAFilter(120), DDGuard(-0.08, 10), label="SMA120+DD8/10d"),
    ]

    # 매수보유 기준
    class Always(Rule):
        name = "매수보유"

        def status(self, idx, date):
            return "BULL"
    bh_w, _ = weekly_returns(idx, Always())
    bh_m = metrics(bh_w, 0)
    bh_y = yearly_mdd(bh_w)
    years = sorted(bh_y.keys())

    print(f"\n🔬 전체기간 ({bh_w.index[0].date()} ~ {bh_w.index[-1].date()}, {len(bh_w)}주)")
    print(f"{'메커니즘':<14}{'총수익':>9}{'CAGR':>8}{'MDD':>8}{'투자%':>7}{'스위칭':>7}")
    rows = []
    for r in rules:
        w, sw = weekly_returns(idx, r)
        m = metrics(w, sw)
        rows.append((r.name, w))
        print(f"{r.name:<14}{m['total']:>+8.0f}%{m['cagr']:>+7.1f}%"
              f"{m['mdd']:>+7.1f}%{m['invested']:>6.0f}%{m['switches']:>7}")
    print(f"{'매수보유':<14}{bh_m['total']:>+8.0f}%{bh_m['cagr']:>+7.1f}%"
          f"{bh_m['mdd']:>+7.1f}%{'100':>6}%{'0':>7}")

    print(f"\n📅 연도별 MDD (낮을수록 방어 좋음 · 하락국면 2018/2020/2022 주목)")
    print("메커니즘".ljust(14) + "".join(f"{y:>7}" for y in years))
    print("매수보유".ljust(14) + "".join(f"{bh_y[y]:>6.0f}%" for y in years))
    for name, w in rows:
        y = yearly_mdd(w)
        print(name.ljust(14) + "".join(f"{y.get(yr, 0):>6.0f}%" for yr in years))

    print("\n판독: 매수보유 대비 하락년 MDD를 확실히 줄이면서(2018/2020/2022) 스위칭이 과하지 않은 "
          "메커니즘이 후보. 전 구간 MDD가 매수보유와 비슷하면 '약한 헤지'.")


if __name__ == "__main__":
    main()
