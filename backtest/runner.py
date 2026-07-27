#!/usr/bin/env python3
"""
backtest/runner.py — 섀도우 전략 백테스트 러너
========================================================
시그널 아카이브(quant_signals/*.json) 시계열 위에서 여러 전략을 나란히 replay하여
구간별/누적 수익률을 계산한다. 실제 종가는 analyze_returns와 동일하게 yf로 조회한다.

설계 원칙 (CLAUDE.md):
  - 객체지향: SignalSeries / PriceProvider / BacktestRunner 로 책임 분리.
  - 읽기 전용: 주문 집행과 무관. 로컬 계산만.
  - 정직한 비용: 리밸런싱 턴오버에 왕복 거래비용을 부과(gross/net 둘 다 보고).

수익률 계산:
  - 리밸런스일 d_i 에 strategy가 universe(d_i)+market(d_i)로 포트폴리오 선택.
  - 다음 리밸런스일 d_{i+1}까지 보유. r_c = P_c(d_{i+1})/P_c(d_i) - 1 (yf 실종가).
  - gross = Σ w_c·r_c  (현금이면 0)
  - 턴오버 비용: drift 반영한 직전 보유비중 대비 |Δw| 합에 side당 비용 부과.

사용:
  dashboard/.venv/bin/python backtest/runner.py
  dashboard/.venv/bin/python backtest/runner.py --cost-per-side 0.002 --universe-field top_10_stocks
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))

import yf  # rambdaA/yf.py 재사용 (analyze_returns와 동일 소스)
from base import Strategy                       # noqa: E402
from baseline import BaselineMomentum126        # noqa: E402
from aggressive import ConcentratedMomentum     # noqa: E402

DEFAULT_ARCHIVE = ROOT / "data" / "s3_archive" / "quant_signals"
COST_PER_SIDE_DEFAULT = 0.002  # side당 0.2% → 왕복 0.4% (현행 백테스트 가정과 정합)


class SignalSeries:
    """quant_signals/*.json 을 날짜순으로 로드해 (date, universe, market)를 제공."""

    def __init__(self, archive_dir: Path = DEFAULT_ARCHIVE,
                 universe_field: str = "universe"):
        self.archive_dir = Path(archive_dir)
        # universe_field: 전체 스냅샷('universe')이 있으면 그것, 없으면 'top_10_stocks'로 폴백
        self.universe_field = universe_field
        self.points: list[dict] = []

    def load(self) -> "SignalSeries":
        for path in sorted(glob.glob(str(self.archive_dir / "*.json"))):
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            uni = d.get(self.universe_field) or d.get("top_10_stocks") or []
            if not uni:
                continue
            date = datetime.strptime(d["updated_at"][:10], "%Y-%m-%d")
            self.points.append({
                "date": date,
                "file": Path(path).name,
                "universe": uni,
                "market": {
                    "market_status": d.get("market_status"),
                    "vix": d.get("vix"),
                    "domestic_dd": d.get("domestic_dd"),
                },
                "used_full_universe": self.universe_field in d,
            })
        self.points.sort(key=lambda p: p["date"])
        return self

    def all_codes(self) -> set[str]:
        codes: set[str] = set()
        for p in self.points:
            for s in p["universe"]:
                codes.add(s["code"])
        return codes

    def date_range(self) -> tuple[datetime, datetime]:
        dates = [p["date"] for p in self.points]
        return min(dates), max(dates)


class PriceProvider:
    """유니버스 종목들의 종가 시계열을 yf로 일괄 조회하고 날짜별 조회를 제공."""

    def __init__(self):
        self._series: dict[str, "object"] = {}

    def load(self, codes: set[str], start: datetime, end: datetime) -> "PriceProvider":
        tickers = [f"{c}.KS" for c in codes]
        raw = yf.download(tickers, start=start - timedelta(days=5),
                          end=end + timedelta(days=3), progress=False, threads=False)
        close = raw["Close"] if "Close" in getattr(raw, "columns", []) else raw
        if hasattr(close.index, "tz") and close.index.tz is not None:
            close.index = close.index.tz_localize(None)
        # 단일 종목이면 Series로 올 수 있음
        import pandas as pd
        if isinstance(close, pd.Series):
            only = tickers[0]
            self._series[only.replace(".KS", "")] = close.dropna()
        else:
            for col in close.columns:
                self._series[str(col).replace(".KS", "")] = close[col].dropna()
        return self

    def price_on_or_before(self, code: str, date: datetime):
        s = self._series.get(code)
        if s is None or len(s) == 0:
            return None
        import pandas as pd
        sub = s[s.index <= pd.Timestamp(date)]
        return float(sub.iloc[-1]) if len(sub) else None


class BacktestRunner:
    """여러 전략을 동일 시그널 시계열 위에서 replay하여 성과를 계산."""

    def __init__(self, series: SignalSeries, prices: PriceProvider,
                 strategies: list[Strategy], cost_per_side: float = COST_PER_SIDE_DEFAULT):
        self.series = series
        self.prices = prices
        self.strategies = strategies
        self.cost_per_side = cost_per_side

    def _interval_return(self, strat: Strategy, point: dict, next_date: datetime,
                         prev_hold: dict) -> dict:
        """point(d_i) 선택 → next_date(d_{i+1})까지 gross/net/턴오버/drift후비중 계산."""
        pf = strat.build_portfolio(point["universe"], point["market"])
        weights = pf["weights"]

        # 각 종목 실현수익률
        per_name_r: dict[str, float] = {}
        usable: dict[str, float] = {}
        for code, w in weights.items():
            p0 = self.prices.price_on_or_before(code, point["date"])
            p1 = self.prices.price_on_or_before(code, next_date)
            if p0 and p1 and p0 > 0:
                per_name_r[code] = p1 / p0 - 1
                usable[code] = w
        # 가격 누락 종목 제외 후 비중 재정규화
        wsum = sum(usable.values())
        if wsum > 0:
            usable = {c: w / wsum for c, w in usable.items()}
        gross = sum(usable[c] * per_name_r[c] for c in usable) if usable else 0.0

        # 턴오버 = 직전 보유(drift 반영) 대비 |Δw| 합, side당 비용 부과
        codes = set(usable) | set(prev_hold)
        turnover = sum(abs(usable.get(c, 0.0) - prev_hold.get(c, 0.0)) for c in codes)
        cost = turnover * self.cost_per_side
        net = gross - cost

        # 다음 구간 진입 비중 = 이번 보유가 수익률만큼 drift한 결과
        if usable and (1 + gross) != 0:
            drifted = {c: usable[c] * (1 + per_name_r[c]) / (1 + gross) for c in usable}
        else:
            drifted = {}
        return {"gross": gross, "cost": cost, "net": net,
                 "turnover": turnover, "cash": pf["cash"],
                 "n": len(usable), "drifted": drifted}

    def run(self) -> dict:
        pts = self.series.points
        results: dict[str, dict] = {}
        for strat in self.strategies:
            intervals = []
            prev_hold: dict = {}
            cum_gross = cum_net = 1.0
            for i in range(len(pts) - 1):
                r = self._interval_return(strat, pts[i], pts[i + 1]["date"], prev_hold)
                cum_gross *= (1 + r["gross"])
                cum_net *= (1 + r["net"])
                intervals.append({
                    "from": pts[i]["date"].strftime("%Y-%m-%d"),
                    "to": pts[i + 1]["date"].strftime("%Y-%m-%d"),
                    **r,
                    "cum_net": cum_net - 1,
                })
                prev_hold = r["drifted"]
            results[strat.name] = {
                "intervals": intervals,
                "total_gross_pct": (cum_gross - 1) * 100,
                "total_net_pct": (cum_net - 1) * 100,
            }
        return results


def main():
    ap = argparse.ArgumentParser(description="섀도우 전략 백테스트 러너")
    ap.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    ap.add_argument("--universe-field", default="universe",
                    help="유니버스 필드명(전체 스냅샷). 없으면 top_10_stocks로 자동 폴백")
    ap.add_argument("--cost-per-side", type=float, default=COST_PER_SIDE_DEFAULT)
    args = ap.parse_args()

    series = SignalSeries(Path(args.archive), args.universe_field).load()
    if len(series.points) < 2:
        print("⚠️ 시그널이 2개 미만이라 구간 계산 불가. quant_signals sync 후 재시도.")
        return

    full = all(p["used_full_universe"] for p in series.points)
    print(f"📂 시그널 {len(series.points)}개 로드 "
          f"({series.points[0]['date']:%Y-%m-%d} ~ {series.points[-1]['date']:%Y-%m-%d})")
    print(f"   유니버스 소스: {'전체 스냅샷' if full else 'top_10 폴백(랭크11~ 접근 전략엔 불충분)'}")

    start, end = series.date_range()
    prices = PriceProvider().load(series.all_codes(), start, end)

    strategies = [BaselineMomentum126(), ConcentratedMomentum()]
    results = BacktestRunner(series, prices, strategies, args.cost_per_side).run()

    for name, res in results.items():
        print(f"\n=== {name} ===")
        print(f"{'구간':<24}{'gross':>9}{'비용':>8}{'net':>9}{'누적net':>10}  종목/현금")
        for iv in res["intervals"]:
            tag = "현금" if iv["cash"] else f"{iv['n']}종목"
            print(f"{iv['from']}→{iv['to']:<12}{iv['gross']*100:>+8.2f}%{iv['cost']*100:>7.2f}%"
                  f"{iv['net']*100:>+8.2f}%{iv['cum_net']*100:>+9.2f}%  {tag}")
        print(f"  ▶ 총 net {res['total_net_pct']:+.2f}%  (gross {res['total_gross_pct']:+.2f}%)")

    print("\n📊 요약")
    for name, res in results.items():
        print(f"  {name:<32} net {res['total_net_pct']:+7.2f}%   gross {res['total_gross_pct']:+7.2f}%")


if __name__ == "__main__":
    main()
