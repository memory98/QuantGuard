#!/usr/bin/env python3
"""
backtest/shadow_forward.py — 섀도우 전진검증 파이프라인 (2단계 검증의 ②)
========================================================
지금까지의 백테스트는 전부 인샘플이다. 이 파이프라인은 매주 실제로 생성되는
유니버스 스냅샷(fix21)을 소비해, 각 섀도우 전략이 '그 주에 무엇을 골랐을지'를
사후지식 없이 기록하고 다음 주 실현수익을 채점한다. 주가 쌓일수록 진짜
out-of-sample 성적표가 된다. 실전 Lambda와 완전 분리, 자본 0.

무엇을 비교하나:
  - 섀도우 전략들(baseline / concentrated / combo-guard baseline)
  - 실전 계좌(latest_signal의 total_equity_checked)
  - 벤치마크(KODEX200 매수보유)

데이터 소스(우선순위):
  1. data/s3_archive/universe/*.json  (fix21 산출, 채점 유니버스 전체 — 권장)
  2. data/s3_archive/quant_signals/*.json (top_10만, 부트스트랩용 폴백)
  → universe 스냅샷은 2026-08-03 첫 정기실행부터 쌓인다. 그 전까진 폴백으로 동작.

한계(정직히):
  - 폴백(top_10) 구간에서는 속도형/레버리지처럼 랭크11~ 또는 별도 유니버스가 필요한
    전략은 채점하지 않는다(universe 스냅샷 쌓이면 자동 편입 예정).
  - 실전 계좌 수익률은 순입출금 0 가정(정식 보정은 scripts/analyze_returns.py).
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "rambdaA"))

import pandas as pd  # noqa: E402
import yf            # noqa: E402
from baseline import BaselineMomentum126      # noqa: E402
from aggressive import ConcentratedMomentum   # noqa: E402
from signal_generator import (                # noqa: E402
    DD_GUARD_TICKER, DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD)

UNIVERSE_DIR = ROOT / "data" / "s3_archive" / "universe"
QUANT_DIR = ROOT / "data" / "s3_archive" / "quant_signals"
ACCOUNT_DIR = ROOT / "data" / "s3_archive" / "latest_signal"
LEDGER_PATH = ROOT / "data" / "shadow_ledger.json"
SMA_WINDOW = 120
COST_PER_SIDE = 0.002


class SnapshotStore:
    """universe/*.json(우선) 또는 quant_signals/*.json(폴백)에서 (date, universe, market)."""

    def load(self):
        src = "universe"
        files = sorted(glob.glob(str(UNIVERSE_DIR / "*.json")))
        if not files:
            src = "quant_signals(폴백 top_10)"
            files = sorted(glob.glob(str(QUANT_DIR / "*.json")))
        points = []
        for p in files:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            uni = d.get("universe") or d.get("top_10_stocks") or []
            if not uni:
                continue
            points.append({
                "date": datetime.strptime(d["updated_at"][:10], "%Y-%m-%d"),
                "universe": uni,
                "market": {"market_status": d.get("market_status"),
                           "vix": d.get("vix"), "domestic_dd": d.get("domestic_dd")},
            })
        points.sort(key=lambda x: x["date"])
        return points, src


class PriceProvider:
    """섀도우 종목 + KODEX200 종가를 yf로 수집, 날짜 as-of 조회."""

    def __init__(self):
        self._s = {}

    def load(self, codes):
        tickers = [f"{c}.KS" for c in codes] + [f"{DD_GUARD_TICKER}.KS"]
        raw = yf.download(list(dict.fromkeys(tickers)), progress=False, threads=False)
        close = raw["Close"] if "Close" in getattr(raw, "columns", []) else raw
        if hasattr(close.index, "tz") and close.index.tz is not None:
            close.index = close.index.tz_localize(None)
        if isinstance(close, pd.Series):
            close = close.to_frame(name=tickers[0])
        for col in close.columns:
            self._s[str(col).replace(".KS", "")] = close[col].dropna()
        return self

    def at(self, code, date):
        s = self._s.get(code)
        if s is None:
            return None
        sub = s[s.index <= pd.Timestamp(date)].dropna()
        return float(sub.iloc[-1]) if len(sub) else None

    def kodex(self):
        return self._s.get(DD_GUARD_TICKER)


def recompute_market(prices: PriceProvider, date, use_sma: bool) -> dict:
    """가드를 가격에서 균일 재계산(배포 이력 착시 제거).

    - use_sma=False → 현행 DD가드(-8%/20일)만
    - use_sma=True  → 콤보: DD OR SMA120 이탈
    스냅샷 저장 market_status를 쓰지 않고 모든 섀도우 전략을 동일 기준으로 채점한다.
    (실제 프로덕션이 그때 무엇을 했는지는 별도 '실계좌' 행이 담는다.)
    """
    kd = prices.kodex()
    status = "BULL"
    if kd is not None:
        s = kd[kd.index <= pd.Timestamp(date)].dropna()
        if len(s) >= DD_GUARD_LOOKBACK:
            dd = float(s.iloc[-1] / s.tail(DD_GUARD_LOOKBACK).max() - 1)
            if dd <= DD_GUARD_THRESHOLD:
                status = "BEAR"
        if use_sma and len(s) >= SMA_WINDOW and float(s.iloc[-1]) < float(s.tail(SMA_WINDOW).mean()):
            status = "BEAR"
    return {"market_status": status}


class AccountReader:
    """latest_signal/*.json의 실계좌 총자산(force_test_mode=False)만 날짜→금액."""

    def load(self):
        eq = {}
        for p in sorted(glob.glob(str(ACCOUNT_DIR / "*.json"))):
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            if d.get("force_test_mode") is not False:
                continue
            e = d.get("total_equity_checked")
            if e:
                eq[datetime.strptime(d["updated_at"][:10], "%Y-%m-%d")] = e
        return eq

    @staticmethod
    def on_or_before(eq, date):
        cand = [d for d in eq if d <= date]
        return eq[max(cand)] if cand else None


def portfolio_return(strat, universe, market, prices, d0, d1, prev_hold):
    pf = strat.build_portfolio(universe, market)
    per_r, usable = {}, {}
    for c, w in pf["weights"].items():
        p0, p1 = prices.at(c, d0), prices.at(c, d1)
        if p0 and p1 and p0 > 0:
            per_r[c] = p1 / p0 - 1
            usable[c] = w
    wsum = sum(usable.values())
    if wsum > 0:
        usable = {c: w / wsum for c, w in usable.items()}
    gross = sum(usable[c] * per_r[c] for c in usable) if usable else 0.0
    turn = sum(abs(usable.get(c, 0) - prev_hold.get(c, 0)) for c in set(usable) | set(prev_hold))
    net = gross - turn * COST_PER_SIDE
    drift = ({c: usable[c] * (1 + per_r[c]) / (1 + gross) for c in usable}
             if usable and (1 + gross) != 0 else {})
    return net, pf["cash"], drift


def main():
    points, src = SnapshotStore().load()
    print(f"📂 스냅샷 소스: {src} — {len(points)}개 "
          f"({points[0]['date']:%Y-%m-%d} ~ {points[-1]['date']:%Y-%m-%d})" if points else
          f"📂 스냅샷 소스: {src} — 0개")
    if len(points) < 2:
        print("⚠️ 스냅샷 2개 미만 → 구간 없음. universe 스냅샷이 쌓이면(2026-08-03~) 누적 시작.")
        return

    codes = {s["code"] for p in points for s in p["universe"]}
    prices = PriceProvider().load(codes)
    account = AccountReader().load()

    # 섀도우 전략: (표시명, 전략객체, use_sma) — 가드는 모두 가격에서 균일 재계산
    specs = [
        ("baseline(DD가드)", BaselineMomentum126(), False),
        ("concentrated(DD가드)", ConcentratedMomentum(), False),
        ("baseline+콤보가드", BaselineMomentum126(), True),
    ]
    ledgers = {name: {"cum": 1.0, "prev": {}, "intervals": []} for name, _, _ in specs}
    bench_cum = 1.0
    acc_cum = 1.0
    rows = []

    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        d0, d1 = a["date"], b["date"]
        row = {"from": d0.strftime("%Y-%m-%d"), "to": d1.strftime("%Y-%m-%d")}

        for name, strat, use_sma in specs:
            market = recompute_market(prices, d0, use_sma)
            net, cash, drift = portfolio_return(strat, a["universe"], market, prices,
                                                d0, d1, ledgers[name]["prev"])
            ledgers[name]["cum"] *= (1 + net)
            ledgers[name]["prev"] = drift
            ledgers[name]["intervals"].append({**row, "net_pct": round(net * 100, 2),
                                               "cash": cash})
            row[name] = round(net * 100, 2)

        # 벤치마크 KODEX200
        k0, k1 = prices.at(DD_GUARD_TICKER, d0), prices.at(DD_GUARD_TICKER, d1)
        b_ret = (k1 / k0 - 1) if (k0 and k1) else 0.0
        bench_cum *= (1 + b_ret)
        row["benchmark"] = round(b_ret * 100, 2)

        # 실전 계좌 (순입출금 0 가정)
        e0 = AccountReader.on_or_before(account, d0)
        e1 = AccountReader.on_or_before(account, d1)
        if e0 and e1 and e0 > 0 and e1 != e0:
            a_ret = e1 / e0 - 1
            acc_cum *= (1 + a_ret)
            row["account"] = round(a_ret * 100, 2)
        else:
            row["account"] = None
        rows.append(row)

    # ── 출력 ──
    print(f"\n📊 섀도우 전진검증 성적표 (구간 {len(rows)}개, 왕복비용 {COST_PER_SIDE*2*100:.1f}%)")
    names = [n for n, _, _ in specs]
    print(f"\n{'구간':<24}" + "".join(f"{n[:16]:>17}" for n in names)
          + f"{'벤치':>9}{'실계좌':>9}")
    for r in rows:
        line = f"{r['from']}→{r['to']:<11}"
        for n in names:
            line += f"{r[n]:>+16.2f}%"
        line += f"{r['benchmark']:>+8.2f}%"
        line += (f"{r['account']:>+8.2f}%" if r['account'] is not None else f"{'—':>9}")
        print(line)

    print(f"\n🏁 누적 (OOS 후보 성적)")
    for n in names:
        print(f"   {n:<24} {(ledgers[n]['cum']-1)*100:>+8.2f}%")
    print(f"   {'benchmark KODEX200':<24} {(bench_cum-1)*100:>+8.2f}%")
    print(f"   {'실전 계좌(순입출금0가정)':<24} {(acc_cum-1)*100:>+8.2f}%")

    # ── 원장 저장 ──
    ledger = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src,
        "intervals": rows,
        "cumulative": {n: round((ledgers[n]["cum"] - 1) * 100, 2) for n in names},
        "benchmark_pct": round((bench_cum - 1) * 100, 2),
        "account_pct": round((acc_cum - 1) * 100, 2),
        "note": "폴백(top_10) 구간은 백테스트 부트스트랩. 진짜 전진 OOS는 universe 스냅샷 누적(2026-08-03~)부터.",
    }
    LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 원장 저장: {LEDGER_PATH.relative_to(ROOT)}")
    print("⚠️ 폴백(top_10) 구간은 부트스트랩. 진짜 전진 OOS는 universe 스냅샷(2026-08-03~)부터 누적.")


if __name__ == "__main__":
    main()
