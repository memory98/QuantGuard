#!/usr/bin/env python3
"""
backtest/longrun.py — 장기 백테스트 (production 로직 재시뮬레이션)
========================================================
시그널 아카이브(몇 주치)로는 전략 우열을 못 가린다. 이 모듈은 fdr 유니버스 +
yf 시세로 과거 전체를 주간 리밸런싱으로 재현해 여러 전략을 나란히 비교한다.

핵심 설계:
  - production 로직 재사용(드리프트 방지): signal_generator의 EXCLUDE_KEYWORDS,
    classify_sector, LOOKBACK, DD_GUARD_* 를 그대로 import.
  - **가드 재시뮬레이션**: 저장 market_status를 쓰지 않고, 각 시점의 실제
    VIX/KODEX200 시세로 BULL/BEAR를 다시 계산 → fix19 배포 이전 구간도 균일 평가.
  - 룩어헤드 없음: 시그널은 금요일 종가 기준, 진입은 다음 거래일(월) 종가.
  - **전략별 유니버스·룩백**: Strategy.universe_tag("normal"|"leverage")와 lookback을
    존중해 백테스트가 전략마다 알맞은 유니버스를 채점한다(속도형 63일/레버리지 2X 지원).

한계(정직히 기록):
  - 유니버스가 '오늘의' 거래대금 상위라 **생존편향** 있음(상폐/거래정지 ETF 누락).
  - yf.py 반환 히스토리 길이에 기간 종속(대략 최근 ~2년).
  - 거래대금 순위 현재값 고정(과거 시점 순위 재구성 아님). → '경향 탐색'용.

사용:
  dashboard/.venv/bin/python backtest/longrun.py --top 80
  dashboard/.venv/bin/python backtest/longrun.py --top 80 --sweep
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))

import pandas as pd  # noqa: E402
import fdr           # noqa: E402
import yf            # noqa: E402
from signal_generator import (  # noqa: E402  — production 로직 재사용
    EXCLUDE_KEYWORDS, classify_sector, LOOKBACK,
    DD_GUARD_TICKER, DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD,
)
from config import VIX_THRESHOLD, VIX_TICKER  # noqa: E402
from baseline import BaselineMomentum126      # noqa: E402
from aggressive import ConcentratedMomentum   # noqa: E402
from fast import FastMomentum63               # noqa: E402
from leverage import LeverageMomentum2X       # noqa: E402
from vol_adjusted import VolAdjustedMomentum   # noqa: E402
from vol_tilted import VolTiltedConcentrated    # noqa: E402

# 레버리지 유니버스 구성용 키워드
_LEV_INCLUDE = ["레버리지", "2X"]
_INVERSE_KW = ["인버스", "곱버스", "-1X", "-2X"]
# 국내 지수 레버리지만: 해외/채권/환율/현금성 제외는 유지
_LEV_KEEP_EXCLUDE = [
    "국채", "채권", "단기", "머니마켓", "MMF", "CD금리", "CD1년", "KOFR",
    "달러", "엔화", "유로", "위안", "선물H", "(H)",
    "미국", "나스닥", "S&P", "글로벌", "선진국", "MSCI",
    "아시아", "신흥국", "이머징", "필라델피아", "차이나", "중국",
]


class UniverseBuilder:
    """fdr 현재 ETF 목록 → 제외키워드 → 거래대금 상위 N. leverage=True면 2X 유니버스."""

    def __init__(self, top: int = 80, leverage: bool = False):
        self.top = top
        self.leverage = leverage

    def _mask(self, names: pd.Series) -> pd.Series:
        if self.leverage:
            return names.apply(
                lambda n: any(k in str(n) for k in _LEV_INCLUDE)
                and not any(k in str(n) for k in _INVERSE_KW)
                and not any(k in str(n) for k in _LEV_KEEP_EXCLUDE))
        return names.apply(lambda n: not any(k in str(n) for k in EXCLUDE_KEYWORDS))

    def build(self) -> pd.DataFrame:
        etf = fdr.StockListing("ETF/KR")
        etf.columns = [c.strip() for c in etf.columns]
        name_col = next(c for c in etf.columns if "이름" in c or "Name" in c)
        code_col = next(c for c in etf.columns if "코드" in c or "Code" in c or "Symbol" in c)
        vol_col = ("amonut" if "amonut" in etf.columns
                   else "quant" if "quant" in etf.columns else None)
        etf = etf.rename(columns={name_col: "Name", code_col: "Code"})
        etf["Code"] = etf["Code"].astype(str).str.zfill(6)
        etf = etf[self._mask(etf["Name"])].copy()
        if vol_col:
            etf["Vol"] = pd.to_numeric(etf[vol_col], errors="coerce").fillna(0)
            etf = etf.nlargest(self.top, "Vol")
        else:
            etf = etf.head(self.top)
        return etf[["Code", "Name"]].reset_index(drop=True)


class PriceMatrix:
    """유니버스 + VIX + KODEX200 종가를 yf로 일괄 수집, 날짜 as-of 조회/모멘텀 제공."""

    def __init__(self):
        self.closes = pd.DataFrame()
        self.vix = None
        self.kodex = None

    def _dl(self, tickers, batch=50):
        frames = []
        for i in range(0, len(tickers), batch):
            b = tickers[i:i + batch]
            raw = yf.download(b, progress=False, threads=False)
            close = raw["Close"] if "Close" in getattr(raw, "columns", []) else raw
            if hasattr(close.index, "tz") and close.index.tz is not None:
                close.index = close.index.tz_localize(None)
            if isinstance(close, pd.Series):
                close = close.to_frame(name=b[0])
            frames.append(close)
            time.sleep(2)
        out = pd.concat(frames, axis=1)
        out.columns = [str(c).replace(".KS", "") for c in out.columns]
        return out.loc[:, ~out.columns.duplicated()].sort_index()

    def load(self, codes) -> "PriceMatrix":
        self.closes = self._dl([f"{c}.KS" for c in codes])
        self.vix = self._dl([VIX_TICKER]).iloc[:, 0].dropna()
        self.kodex = self._dl([f"{DD_GUARD_TICKER}.KS"]).iloc[:, 0].dropna()
        return self

    def asof(self, series, date):
        s = series[series.index <= date].dropna()
        return s if len(s) else None

    def momentum(self, code, date, lookback: int = LOOKBACK):
        if code not in self.closes.columns:
            return None
        s = self.closes[code]
        s = s[s.index <= date].dropna()
        if len(s) < int(lookback * 1.5) or len(s) <= lookback:
            return None
        current = float(s.iloc[-1])
        base = float(s.iloc[-1 - lookback])
        if base <= 0:
            return None
        return current, current / base - 1

    def price_at(self, code, date):
        s = self.asof(self.closes[code], date) if code in self.closes.columns else None
        return float(s.iloc[-1]) if s is not None else None

    def volatility(self, code, date, window: int = 63):
        """date까지 최근 window 거래일 일간수익률 표준편차(변동성조정 전략용)."""
        if code not in self.closes.columns:
            return None
        s = self.closes[code]
        s = s[s.index <= date].dropna()
        if len(s) < window + 1:
            return None
        rets = s.pct_change().dropna().tail(window)
        v = float(rets.std())
        return v if v > 0 else None


class GuardSimulator:
    """각 시점의 실제 VIX/KODEX200 시세로 market_status(BULL/BEAR) 재계산.

    dd_threshold: 국내 드로다운 BEAR 임계(기본=production DD_GUARD_THRESHOLD).
                  스윕 시 이 값만 바꿔 주입한다. 재진입은 대칭(이 값 위로 회복하면 BULL).
    """

    def __init__(self, prices: PriceMatrix, dd_threshold: float = DD_GUARD_THRESHOLD,
                 sma_window: int = None):
        self.p = prices
        self.dd_threshold = dd_threshold
        # sma_window: 설정 시 KODEX200 종가 < N일 이동평균이면 BEAR(추세필터) 추가.
        #   콤보 가드(SMA120 OR DD) 검증용. None이면 현행(VIX+DD)과 동일.
        self.sma_window = sma_window

    def market(self, date) -> dict:
        status = "BULL"
        vix_s = self.p.asof(self.p.vix, date)
        vix = float(vix_s.iloc[-1]) if vix_s is not None else None
        if vix is not None and vix > VIX_THRESHOLD:
            status = "BEAR"
        kd = self.p.kodex[self.p.kodex.index <= date].dropna()
        dd = None
        if len(kd) >= DD_GUARD_LOOKBACK:
            dd = float(kd.iloc[-1] / kd.tail(DD_GUARD_LOOKBACK).max() - 1)
            if dd <= self.dd_threshold:
                status = "BEAR"
        if self.sma_window and len(kd) >= self.sma_window:
            if float(kd.iloc[-1]) < float(kd.tail(self.sma_window).mean()):
                status = "BEAR"
        return {"market_status": status, "vix": vix, "domestic_dd": dd}


class LongBacktest:
    """주간 리밸런싱 replay → 전략별 자산곡선 + 지표. 전략별 유니버스/룩백 존중."""

    def __init__(self, universes: dict, prices: PriceMatrix, guard: GuardSimulator,
                 strategies: list, cost_per_side: float = 0.002):
        self.universes = universes  # {"normal": df, "leverage": df}
        self.p = prices
        self.guard = guard
        self.strategies = strategies
        self.cost = cost_per_side
        self.name_maps = {tag: dict(zip(df["Code"], df["Name"]))
                          for tag, df in universes.items()}
        self._cache = {}

    def _fridays(self):
        idx = self.p.closes.index
        fri = [d for d in idx if pd.Timestamp(d).weekday() == 4]
        # 공정성: 가장 긴 룩백 전략도 첫 주부터 min-history(≈lookback*1.5)를 충족하도록
        # 공통 시작점을 최장 룩백 기준으로 잡는다(126일 전략의 189일 burn-in 반영).
        warmup = int(LOOKBACK * 1.5) + 2
        start_ok = idx[min(warmup, len(idx) - 1)]
        return [d for d in fri if d >= start_ok]

    def _scored(self, date, tag, lookback):
        key = (tag, lookback, date)
        if key in self._cache:
            return self._cache[key]
        nm = self.name_maps[tag]
        out = []
        for code in self.universes[tag]["Code"]:
            m = self.p.momentum(code, date, lookback)
            if m is None:
                continue
            price, mom = m
            name = nm.get(code, code)
            out.append({"code": code, "name": name, "price": price,
                        "momentum": mom, "sector": classify_sector(name),
                        "vol": self.p.volatility(code, date, 63)})
        self._cache[key] = out
        return out

    def run(self) -> dict:
        fridays = self._fridays()
        idx = self.p.closes.index
        results = {}
        for strat in self.strategies:
            equity_gross = equity_net = 1.0
            curve = [1.0]
            wins = trials = 0
            prev_hold = {}
            for i in range(len(fridays) - 1):
                f0, f1 = fridays[i], fridays[i + 1]
                entry = idx[idx > f0]
                exit_ = idx[idx > f1]
                if len(entry) == 0 or len(exit_) == 0:
                    continue
                d_entry, d_exit = entry[0], exit_[0]
                market = self.guard.market(f0)
                scored = self._scored(f0, strat.universe_tag, strat.lookback)
                pf = strat.build_portfolio(scored, market)
                weights = pf["weights"]

                per_r, usable = {}, {}
                for c, w in weights.items():
                    p0, p1 = self.p.price_at(c, d_entry), self.p.price_at(c, d_exit)
                    if p0 and p1 and p0 > 0:
                        per_r[c] = p1 / p0 - 1
                        usable[c] = w
                wsum = sum(usable.values())
                if wsum > 0:
                    usable = {c: w / wsum for c, w in usable.items()}
                gross = sum(usable[c] * per_r[c] for c in usable) if usable else 0.0
                turn = sum(abs(usable.get(c, 0) - prev_hold.get(c, 0))
                           for c in set(usable) | set(prev_hold))
                net = gross - turn * self.cost

                equity_gross *= (1 + gross)
                equity_net *= (1 + net)
                curve.append(equity_net)
                if not pf["cash"]:
                    trials += 1
                    wins += 1 if gross > 0 else 0
                prev_hold = ({c: usable[c] * (1 + per_r[c]) / (1 + gross) for c in usable}
                             if usable and (1 + gross) != 0 else {})

            results[strat.name] = self._metrics(curve, equity_gross, wins, trials,
                                                 fridays[0], fridays[-1])
        return results

    @staticmethod
    def _metrics(curve, eq_gross, wins, trials, d0, d1):
        s = pd.Series(curve)
        peak = s.cummax()
        mdd = float((s / peak - 1).min())
        days = max((d1 - d0).days, 1)
        cagr = s.iloc[-1] ** (365 / days) - 1
        return {
            "total_net_pct": (s.iloc[-1] - 1) * 100,
            "total_gross_pct": (eq_gross - 1) * 100,
            "cagr_pct": cagr * 100,
            "mdd_pct": mdd * 100,
            "win_rate_pct": (wins / trials * 100) if trials else 0.0,
            "weeks": len(curve) - 1,
            "invested_weeks": trials,
        }


def benchmark(prices: PriceMatrix, d0, d1) -> dict:
    s = prices.kodex
    p0 = prices.asof(s, d0).iloc[-1]
    p1 = prices.asof(s, d1).iloc[-1]
    seg = s[(s.index >= d0) & (s.index <= d1)]
    peak = seg.cummax()
    mdd = float((seg / peak - 1).min())
    days = max((d1 - d0).days, 1)
    cagr = (p1 / p0) ** (365 / days) - 1
    return {"total_net_pct": (p1 / p0 - 1) * 100, "cagr_pct": cagr * 100, "mdd_pct": mdd * 100}


def _build_data(top):
    print(f"📋 유니버스 구성 (일반 상위 {top} + 레버리지 2X, production 제외키워드)...")
    uni_norm = UniverseBuilder(top).build()
    uni_lev = UniverseBuilder(top, leverage=True).build()
    print(f"   일반 {len(uni_norm)}개 / 레버리지 {len(uni_lev)}개")
    all_codes = list(dict.fromkeys(list(uni_norm["Code"]) + list(uni_lev["Code"])))
    print("📡 시세 수집 (yf, 배치)...")
    prices = PriceMatrix().load(all_codes)
    span0, span1 = prices.closes.index[0], prices.closes.index[-1]
    print(f"   히스토리: {span0:%Y-%m-%d} ~ {span1:%Y-%m-%d} ({len(prices.closes)}거래일)")
    return {"normal": uni_norm, "leverage": uni_lev}, prices


def main():
    ap = argparse.ArgumentParser(description="장기 백테스트 (production 로직 재시뮬)")
    ap.add_argument("--top", type=int, default=80, help="거래대금 상위 N ETF 유니버스")
    ap.add_argument("--cost-per-side", type=float, default=0.002)
    ap.add_argument("--sweep", action="store_true",
                    help="DD가드 임계값 스윕(baseline 전략, 여러 임계 비교)")
    ap.add_argument("--sweep-thresholds", type=str, default="-0.05,-0.06,-0.08,-0.10,-0.12",
                    help="쉼표구분 임계값 목록 (현행 -0.08)")
    args = ap.parse_args()

    universes, prices = _build_data(args.top)

    if args.sweep:
        thresholds = [float(t) for t in args.sweep_thresholds.split(",")]
        probe = LongBacktest(universes, prices, GuardSimulator(prices), [BaselineMomentum126()])
        fr = probe._fridays()
        bench = benchmark(prices, fr[0], fr[-1])
        print(f"\n🔬 DD가드 임계값 스윕 (baseline·{fr[0]:%Y-%m-%d}~{fr[-1]:%Y-%m-%d}·{len(fr)-1}주)")
        print(f"{'임계값':>8}{'총수익(net)':>12}{'CAGR':>9}{'MDD':>9}{'투자주수':>9}   비고")
        for th in thresholds:
            guard = GuardSimulator(prices, dd_threshold=th)
            m = LongBacktest(universes, prices, guard, [BaselineMomentum126()],
                             args.cost_per_side).run()["baseline_momentum_126"]
            tag = " ← 현행" if abs(th - DD_GUARD_THRESHOLD) < 1e-9 else ""
            print(f"{th*100:>6.0f}%{m['total_net_pct']:>+11.1f}%{m['cagr_pct']:>+8.1f}%"
                  f"{m['mdd_pct']:>+8.1f}%{m['invested_weeks']:>8}{tag}")
        print(f"{'매수보유':>8}{bench['total_net_pct']:>+11.1f}%{bench['cagr_pct']:>+8.1f}%"
              f"{bench['mdd_pct']:>+8.1f}%{'—':>9}   벤치마크(가드 없음)")
        print("\n판독법: 임계가 낮을수록(예민) 투자주수↓·MDD↓·수익↓(헛발질/기회손실). "
              "여러 구간에서 robust하게 우월한 값만 프로덕션 반영.")
        return

    strategies = [BaselineMomentum126(), ConcentratedMomentum(),
                  FastMomentum63(), LeverageMomentum2X(),
                  VolAdjustedMomentum(), VolTiltedConcentrated()]
    bt = LongBacktest(universes, prices, GuardSimulator(prices), strategies, args.cost_per_side)
    res = bt.run()

    fr = bt._fridays()
    bench = benchmark(prices, fr[0], fr[-1])
    print(f"\n🗓  백테스트 구간: {fr[0]:%Y-%m-%d} ~ {fr[-1]:%Y-%m-%d} ({len(fr)-1}주)")
    print(f"\n{'전략':<32}{'총수익(net)':>11}{'CAGR':>9}{'MDD':>9}{'승률':>8}{'투자주수':>8}")
    for name, m in res.items():
        print(f"{name:<32}{m['total_net_pct']:>+10.1f}%{m['cagr_pct']:>+8.1f}%"
              f"{m['mdd_pct']:>+8.1f}%{m['win_rate_pct']:>7.0f}%{m['invested_weeks']:>7}")
    print(f"{'[벤치마크] KODEX200 매수보유':<32}{bench['total_net_pct']:>+10.1f}%"
          f"{bench['cagr_pct']:>+8.1f}%{bench['mdd_pct']:>+8.1f}%{'—':>8}{'—':>8}")
    print("\n⚠️ 생존편향/기간종속 한계 있음 — 경향 탐색용. 실전 판단은 섀도우 전진검증 병행.")


if __name__ == "__main__":
    main()
