#!/usr/bin/env python3
"""
backtest/guard_sweep.py — DD가드 임계값 다구간(워크포워드) 검증
========================================================
'가드가 몇 %가 최선인가'를 한 구간이 아니라 여러 국면에서 본다.

핵심 설계:
  - **가드 격리**: KODEX200을 BULL이면 보유·BEAR이면 현금으로 스위칭하는 단순 전략으로
    임계값만 스윕한다. 종목선택/생존편향 노이즈를 제거하고 가드 자체만 평가.
  - **10년 롱히스토리**: production yf.py는 range=2y 하드캡이라 건드리지 않고, 백테스트
    전용 페처로 range=10y(2016~)를 받아 2020 코로나·2022 하락장 등 다른 국면을 포함.
  - **연도별 분해**: 전체 기간 승자 + 각 연도별 승자를 함께 봐서 robust함을 판정한다.
  - production 상수 재사용: DD_GUARD_LOOKBACK(20), VIX_THRESHOLD(30).

사용:
  dashboard/.venv/bin/python backtest/guard_sweep.py
  dashboard/.venv/bin/python backtest/guard_sweep.py --thresholds -0.05,-0.08,-0.10,-0.12
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rambdaA"))
from signal_generator import DD_GUARD_TICKER, DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD  # noqa: E402
from config import VIX_THRESHOLD  # noqa: E402


def fetch(symbol: str, rng: str = "10y") -> pd.Series:
    """백테스트 전용 롱히스토리 페처(yf.py 미변경). adjclose 우선."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    c = raw["chart"]["result"][0]
    ts = c["timestamp"]
    try:
        closes = c["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError):
        closes = c["indicators"]["quote"][0]["close"]
    idx = pd.to_datetime([pd.Timestamp(t, unit="s").normalize() for t in ts])
    return pd.Series(closes, index=idx).dropna()


class IndexGuardSweep:
    """KODEX200 보유/현금 스위칭 전략으로 DD 임계값을 스윕."""

    def __init__(self, idx: pd.Series, vix: pd.Series,
                 lookback: int = DD_GUARD_LOOKBACK, cost_per_switch: float = 0.001):
        self.idx = idx
        self.vix = vix
        self.lookback = lookback
        self.cost = cost_per_switch

    def _fridays(self):
        fri = [d for d in self.idx.index if pd.Timestamp(d).weekday() == 4]
        return [d for d in fri if d >= self.idx.index[self.lookback + 1]]

    def _status(self, date, dd_th):
        """BEAR 판정: VIX>30 또는 KODEX200 20일 고점대비 dd_th 이하."""
        v = self.vix[self.vix.index <= date].dropna()
        if len(v) and float(v.iloc[-1]) > VIX_THRESHOLD:
            return "BEAR"
        s = self.idx[self.idx.index <= date].dropna()
        if len(s) >= self.lookback:
            dd = float(s.iloc[-1] / s.tail(self.lookback).max() - 1)
            if dd <= dd_th:
                return "BEAR"
        return "BULL"

    def weekly_returns(self, dd_th) -> pd.Series:
        """주간 전략 수익률 시리즈(인덱스=구간말일). BEAR면 0(현금), 스위칭 시 비용."""
        idx = self.idx.index
        fri = self._fridays()
        out_r, out_d = [], []
        prev_pos = 0
        for i in range(len(fri) - 1):
            f0, f1 = fri[i], fri[i + 1]
            entry = idx[idx > f0]
            exit_ = idx[idx > f1]
            if len(entry) == 0 or len(exit_) == 0:
                continue
            d_en, d_ex = entry[0], exit_[0]
            pos = 0 if self._status(f0, dd_th) == "BEAR" else 1
            r = float(self.idx.loc[d_ex] / self.idx.loc[d_en] - 1)
            wr = pos * r
            if pos != prev_pos:          # 진입/청산 스위치 비용
                wr -= self.cost
            out_r.append(wr)
            out_d.append(d_ex)
            prev_pos = pos
        return pd.Series(out_r, index=pd.to_datetime(out_d))

    @staticmethod
    def metrics(weekly: pd.Series) -> dict:
        eq = (1 + weekly).cumprod()
        mdd = float((eq / eq.cummax() - 1).min())
        days = max((weekly.index[-1] - weekly.index[0]).days, 1)
        cagr = float(eq.iloc[-1]) ** (365 / days) - 1
        invested = float((weekly != 0).mean()) * 100  # 대략적 투자 비중(현금주=0)
        return {"total": (float(eq.iloc[-1]) - 1) * 100, "cagr": cagr * 100,
                "mdd": mdd * 100, "invested": invested}

    @staticmethod
    def yearly(weekly: pd.Series) -> dict:
        res = {}
        for yr, grp in weekly.groupby(weekly.index.year):
            eq = (1 + grp).cumprod()
            mdd = float((eq / eq.cummax() - 1).min())
            res[yr] = {"ret": (float(eq.iloc[-1]) - 1) * 100, "mdd": mdd * 100}
        return res


def main():
    ap = argparse.ArgumentParser(description="DD가드 임계값 다구간 검증")
    ap.add_argument("--thresholds", type=str, default="-0.05,-0.06,-0.08,-0.10,-0.12")
    ap.add_argument("--range", type=str, default="10y")
    args = ap.parse_args()
    thresholds = [float(t) for t in args.thresholds.split(",")]

    print(f"📡 롱히스토리 수집 (KODEX200 + VIX, range={args.range})...")
    idx = fetch(f"{DD_GUARD_TICKER}.KS", args.range)
    vix = fetch("%5EVIX", args.range)
    print(f"   KODEX200 {len(idx)}일  {idx.index[0].date()} ~ {idx.index[-1].date()}")

    eng = IndexGuardSweep(idx, vix)

    # 매수보유(가드 없음) 기준
    bh = eng.weekly_returns(-9.99)  # 절대 BEAR 안 되는 임계 → 항상 보유
    bh_m = eng.metrics(bh)

    print(f"\n🔬 전체기간 스윕 ({bh.index[0].date()} ~ {bh.index[-1].date()}, {len(bh)}주)")
    print(f"{'임계값':>8}{'총수익':>11}{'CAGR':>9}{'MDD':>9}{'투자비중':>9}   비고")
    per_th = {}
    for th in thresholds:
        w = eng.weekly_returns(th)
        m = eng.metrics(w)
        per_th[th] = eng.yearly(w)
        tag = " ← 현행" if abs(th - DD_GUARD_THRESHOLD) < 1e-9 else ""
        print(f"{th*100:>6.0f}%{m['total']:>+10.0f}%{m['cagr']:>+8.1f}%"
              f"{m['mdd']:>+8.1f}%{m['invested']:>8.0f}%{tag}")
    print(f"{'매수보유':>8}{bh_m['total']:>+10.0f}%{bh_m['cagr']:>+8.1f}%"
          f"{bh_m['mdd']:>+8.1f}%{'100':>8}%   가드 없음")

    # 연도별 MDD (가드의 방어력이 국면마다 robust한지)
    bh_y = eng.yearly(bh)
    years = sorted(bh_y.keys())
    print(f"\n📅 연도별 MDD (가드 방어력 · 낮을수록 좋음)")
    header = "  국면".ljust(10) + "".join(f"{y:>8}" for y in years)
    print(header)
    print(f"{'매수보유':<10}" + "".join(f"{bh_y[y]['mdd']:>7.0f}%" for y in years))
    for th in thresholds:
        row = "".join(f"{per_th[th].get(y, {}).get('mdd', 0):>7.0f}%" for y in years)
        tag = "←현행" if abs(th - DD_GUARD_THRESHOLD) < 1e-9 else ""
        print(f"{('DD '+str(int(th*100))+'%'):<10}" + row + f"  {tag}")

    print(f"\n📅 연도별 수익률 (가드가 상승장 수익을 얼마나 깎나)")
    print(header)
    print(f"{'매수보유':<10}" + "".join(f"{bh_y[y]['ret']:>7.0f}%" for y in years))
    for th in thresholds:
        row = "".join(f"{per_th[th].get(y, {}).get('ret', 0):>7.0f}%" for y in years)
        tag = "←현행" if abs(th - DD_GUARD_THRESHOLD) < 1e-9 else ""
        print(f"{('DD '+str(int(th*100))+'%'):<10}" + row + f"  {tag}")

    print("\n판독: 하락국면(예 2022) MDD를 꾸준히 줄이면서 상승국면 수익을 덜 깎는 임계가 robust. "
          "한 해만 좋은 값은 과최적화.")


if __name__ == "__main__":
    main()
