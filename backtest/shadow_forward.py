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
sys.path.insert(0, str(ROOT / "backtest"))

import pandas as pd  # noqa: E402
import yf            # noqa: E402
from baseline import BaselineMomentum126      # noqa: E402
from aggressive import ConcentratedMomentum   # noqa: E402
from vol_tilted import VolTiltedConcentrated   # noqa: E402
from signal_generator import (                # noqa: E402
    DD_GUARD_TICKER, DD_GUARD_LOOKBACK, DD_GUARD_THRESHOLD)
from guards import (                          # noqa: E402
    DDGuard, ComboGuard, SigmaDDGuard, DailyCircuitBreaker, K_SIGMA_RECAL, MarketGuard)

UNIVERSE_DIR = ROOT / "data" / "s3_archive" / "universe"
QUANT_DIR = ROOT / "data" / "s3_archive" / "quant_signals"
ACCOUNT_DIR = ROOT / "data" / "s3_archive" / "latest_signal"
LEDGER_PATH = ROOT / "data" / "shadow_ledger.json"
SMA_WINDOW = 120
from costs import DEFAULT_COST, split_turnover  # noqa: E402

COST = DEFAULT_COST            # 비용 가정 단일 소스(backtest/costs.py)
COST_PER_SIDE = COST.entry     # 기존 이름 유지(출력 문구 등에서 사용)


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

    def volatility(self, code, date, window=63):
        """변동성조정 전략용: date까지 최근 window 거래일 일간수익률 표준편차."""
        s = self._s.get(code)
        if s is None:
            return None
        sub = s[s.index <= pd.Timestamp(date)].dropna()
        if len(sub) < window + 1:
            return None
        v = float(sub.pct_change().dropna().tail(window).std())
        return v if v > 0 else None

    def kodex(self):
        return self._s.get(DD_GUARD_TICKER)


def recompute_market(prices: PriceProvider, date, guard) -> dict:
    """가드를 가격에서 균일 재계산(배포 이력 착시 제거).

    스냅샷 저장 market_status를 쓰지 않고 모든 섀도우 전략을 동일 기준으로 채점한다.
    (실제 프로덕션이 그때 무엇을 했는지는 별도 '실계좌' 행이 담는다.)

    [2026-08-23] `use_sma: bool` → 가드 객체(backtest/guards.py). 후보가 늘어날 때
    불리언 조합이 폭발하는 것을 막고, 각 후보의 사전고정 파라미터를 원장에 박는다.
    """
    v = guard.evaluate(prices, date)
    # reason은 전략이 읽지 않는다(동작 무변경). 원장에 "왜 대피했나"를 남기기 위한 것.
    return {"market_status": v.status, "guard_reason": v.reason}


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
        """as-of 조회 → (스냅샷 날짜, 총자산). 없으면 (None, None).

        날짜를 함께 돌려주는 이유: 구간 양끝이 '서로 다른 스냅샷'인지 판별해야
        한다. 금액만 보면 BEAR 현금 대피 중 자산이 한 푼도 안 변한 진짜 0% 수익과,
        스냅샷 미갱신으로 같은 값을 두 번 읽은 결측을 구분할 수 없다.
        """
        cand = [d for d in eq if d <= date]
        if not cand:
            return None, None
        k = max(cand)
        return k, eq[k]

    @classmethod
    def interval_return(cls, eq, d0, d1):
        """구간 실계좌 수익률(소수). 채점 불가면 None.

        유효 조건은 '금액이 다른가'가 아니라 '서로 다른 스냅샷인가'다.
        100% 현금 대피 중에는 자산이 정확히 같아도 그것이 진짜 0% 수익이다.
        """
        t0, e0 = cls.on_or_before(eq, d0)
        t1, e1 = cls.on_or_before(eq, d1)
        if e0 and e1 and e0 > 0 and t0 != t1:
            return e1 / e0 - 1
        return None


def portfolio_return(strat, universe, market, prices, d0, d1, prev_hold, exit_date=None):
    """구간 수익률(비용 차감). exit_date가 있으면 그날 전량 청산 후 구간 끝까지 현금.

    exit_date=None이면 기존 동작과 완전히 동일하다(일간 차단기 후보를 넣으면서도
    기존 후보들의 과거 성적이 한 자리도 바뀌면 안 되므로 이 등가성이 중요하다).
    """
    pf = strat.build_portfolio(universe, market)
    d_end = exit_date or d1
    per_r, usable = {}, {}
    for c, w in pf["weights"].items():
        p0, p1 = prices.at(c, d0), prices.at(c, d_end)
        if p0 and p1 and p0 > 0:
            per_r[c] = p1 / p0 - 1
            usable[c] = w
    wsum = sum(usable.values())
    if wsum > 0:
        usable = {c: w / wsum for c, w in usable.items()}
    gross = sum(usable[c] * per_r[c] for c in usable) if usable else 0.0
    buy_turn, sell_turn = split_turnover(usable, prev_hold)
    exited = bool(exit_date) and bool(usable)
    if exited:
        # 비상 청산: 보유 전량을 구간 중간에 팔았으므로 '매도' 회전이 한 번 더 발생한다.
        sell_turn += sum(usable.values())
    net = gross - COST.on_turnover(buy_turn, sell_turn)
    if exited:
        # 청산 후 구간 끝까지 현금 → 다음 구간 시작 보유는 없음
        drift, cash_flag = {}, 1.0
    else:
        drift = ({c: usable[c] * (1 + per_r[c]) / (1 + gross) for c in usable}
                 if usable and (1 + gross) != 0 else {})
        cash_flag = pf["cash"]
    return net, cash_flag, drift, exited


def interval_dd_min(prices, d0, d1):
    """구간 (d0, d1] 중 KODEX200 20일 낙폭의 **최저치**(가장 음수). 표본 부족이면 None.

    AUDIT ③ STEP D의 폭락 사건 정의(-16% 진입 / -8% 회복)를 나중에 자동 판정하려면
    BEAR가 아닌 구간의 낙폭도 필요하다. guard_reasons는 BEAR일 때만 남으므로 별도로 센다.
    """
    kd = prices.kodex()
    if kd is None:
        return None
    s = kd.dropna()
    window = s[(s.index > pd.Timestamp(d0)) & (s.index <= pd.Timestamp(d1))]
    vals = [dd for ts in window.index
            if (dd := MarketGuard._drawdown(s[s.index <= ts])) is not None]
    return round(min(vals), 6) if vals else None


# 원장에 저장될 필드 계약. **여기에 없는 곳에 기록하면 파일에 안 남는다.**
# (2026-08-26 사고: guard_reason을 ledgers[name]["intervals"]에만 넣었는데 그 구조는
#  저장되지 않아, "원장에 대피 사유가 남는다"는 주장이 실제로는 거짓이었다.)
LEDGER_REQUIRED_KEYS = ("generated_at", "source", "intervals", "cumulative",
                        "guard_specs", "cost_model", "guard_reasons")


def build_ledger(src, rows, names, ledgers, specs, guard_reasons) -> dict:
    """저장될 원장의 본문을 조립한다(파일 I/O 없음 — 계약 테스트를 위해 분리)."""
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src,
        "intervals": rows,
        "cumulative": {n: round((ledgers[n]["cum"] - 1) * 100, 2) for n in names},
        # 후보별 가드 파라미터를 매번 원장에 박는다. 사후에 조용히 바뀌면 원장 비교로 드러난다.
        "guard_specs": {n: g.describe() for n, _, g in specs},
        "cost_model": COST.describe(),
        # 대피/청산 사유. 없으면 "이 후보가 왜 졌나"를 물을 때마다 과거를 재생해야 한다.
        "guard_reasons": guard_reasons,
    }


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

    # 섀도우 전략: (표시명, 전략객체, 가드객체) — 가드는 모두 가격에서 균일 재계산
    # [2026-08-23 추가] 아래 두 후보는 2026-08 재진입 국면 관측용. 임계는 사전고정이며
    # 사후에 바꾸지 않는다(근거는 backtest/guards.py 주석). 프로덕션 무반영.
    specs = [
        ("baseline(DD가드)", BaselineMomentum126(), DDGuard()),
        ("concentrated(DD가드)", ConcentratedMomentum(), DDGuard()),
        ("voltilt(DD가드)", VolTiltedConcentrated(), DDGuard()),   # 재설계 공격형(리스크조정 선별+집중)
        ("baseline+콤보가드", BaselineMomentum126(), ComboGuard()),
        ("baseline+σ임계가드", BaselineMomentum126(), SigmaDDGuard()),
        ("baseline+일간비상", BaselineMomentum126(), DailyCircuitBreaker()),
        # σ가드 계수 2계열을 병행 채점한다 — 어느 추정량이 옳은지 고르지 않고 실측에 맡긴다.
        ("baseline+σ재보정", BaselineMomentum126(), SigmaDDGuard(k=K_SIGMA_RECAL)),
    ]
    ledgers = {name: {"cum": 1.0, "prev": {}, "intervals": []} for name, _, _ in specs}
    bench_cum = 1.0
    acc_cum = 1.0
    acc_intervals = 0        # 실계좌가 실제로 채점된 구간 수(0이면 누적은 '없음'이지 0%가 아니다)
    rows = []

    guard_reasons: dict[str, dict] = {}   # "from→to" → {후보: 대피 근거}. 원장에 저장된다.

    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        d0, d1 = a["date"], b["date"]
        row = {"from": d0.strftime("%Y-%m-%d"), "to": d1.strftime("%Y-%m-%d")}
        span = f"{row['from']}→{row['to']}"
        # AUDIT ③ STEP D: 폭락 사건(-16% 진입) 판정에 쓸 구간별 낙폭 최저치.
        # 관측 전용 — 어떤 후보의 수익률에도 관여하지 않는다.
        row["dd_min"] = interval_dd_min(prices, d0, d1)
        # 변동성조정 전략용: 유니버스에 vol 필드 주입(다른 전략은 무시)
        uni = [{**s, "vol": prices.volatility(s["code"], d0)} for s in a["universe"]]

        for name, strat, guard in specs:
            market = recompute_market(prices, d0, guard)
            # 주간 판정이 BULL일 때만 구간 중 비상 청산을 볼 의미가 있다(BEAR면 이미 현금).
            exit_date = (guard.intra_exit(prices, d0, d1)
                         if market["market_status"] == "BULL" else None)
            net, cash, drift, exited = portfolio_return(
                strat, uni, market, prices, d0, d1, ledgers[name]["prev"], exit_date)
            ledgers[name]["cum"] *= (1 + net)
            ledgers[name]["prev"] = drift
            entry = {**row, "net_pct": round(net * 100, 2), "cash": cash}
            if market["market_status"] == "BEAR":
                # 왜 대피했는지를 원장에 남긴다 — 없으면 매번 과거를 재생해야 한다.
                # entry(=ledgers[name])는 저장되지 않으므로 guard_reasons에도 반드시 넣는다.
                entry["guard_reason"] = market["guard_reason"]
                guard_reasons.setdefault(span, {})[name] = market["guard_reason"]
            if exited:
                entry["intra_exit"] = exit_date.strftime("%Y-%m-%d")
                guard_reasons.setdefault(span, {})[f"{name}::intra_exit"] = \
                    exit_date.strftime("%Y-%m-%d")
                print(f"   ⚡ [{name}] {entry['intra_exit']} 비상 청산 발동")
            ledgers[name]["intervals"].append(entry)
            row[name] = round(net * 100, 2)

        # 벤치마크 KODEX200
        k0, k1 = prices.at(DD_GUARD_TICKER, d0), prices.at(DD_GUARD_TICKER, d1)
        b_ret = (k1 / k0 - 1) if (k0 and k1) else 0.0
        bench_cum *= (1 + b_ret)
        row["benchmark"] = round(b_ret * 100, 2)

        # 실전 계좌 (순입출금 0 가정)
        a_ret = AccountReader.interval_return(account, d0, d1)
        if a_ret is not None:
            acc_cum *= (1 + a_ret)
            acc_intervals += 1
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
    account_pct = round((acc_cum - 1) * 100, 2) if acc_intervals else None
    print(f"   {'실전 계좌(순입출금0가정)':<24} "
          + (f"{account_pct:>+8.2f}%" if account_pct is not None
             else f"{'데이터 없음':>9} (채점된 구간 0개)"))

    # ── 원장 저장 ──
    ledger = {
        **build_ledger(src, rows, names, ledgers, specs, guard_reasons),
        "benchmark_pct": round((bench_cum - 1) * 100, 2),
        "account_pct": account_pct,          # None = 채점 가능한 구간 없음(0%와 구분)
        "account_intervals": acc_intervals,
        "note": "폴백(top_10) 구간은 백테스트 부트스트랩. 진짜 전진 OOS는 universe 스냅샷 누적(2026-08-03~)부터.",
    }
    LEDGER_PATH.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 원장 저장: {LEDGER_PATH.relative_to(ROOT)}")
    print("⚠️ 폴백(top_10) 구간은 부트스트랩. 진짜 전진 OOS는 universe 스냅샷(2026-08-03~)부터 누적.")


if __name__ == "__main__":
    main()
