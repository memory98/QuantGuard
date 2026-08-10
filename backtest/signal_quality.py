#!/usr/bin/env python3
"""
backtest/signal_quality.py — 신호 품질 주간 관측 (#OPEN-S / #OPEN-B)
====================================================================
AUDIT.md ③ 주간 신호 검증 루프의 STEP A(기록)와 STEP B(판정)를 구현한다.

관측 대상 두 가지:
  1) **#OPEN-S 순위 예측력** — 직전 주 momentum 순위 ↔ 그 주 실현수익의 IC(순위상관),
     분위별 수익, top10−유니버스 스프레드.
  2) **#OPEN-B 가드 대리지표 타당성** — top10 동일가중 ↔ KODEX200 상관/베타.
     DD가드가 KODEX200으로 포트 위험을 대리하는 전제가 아직 유효한지.

**매매 로직을 건드리지 않는다.** 프로덕션 Lambda와 무관하며, S3 스냅샷을 읽어
관측치를 원장(data/signal_quality_ledger.json)에 누적할 뿐이다.

⚠️ 이 스크립트의 출력으로 파라미터를 튜닝하지 않는다(signal-tuning-freeze).
   IC는 주간 노이즈가 신호보다 크다. 판정은 STEP B의 사전 고정 기준으로만,
   최소 26주 누적 후에.

실행: python backtest/signal_quality.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backtest"))
sys.path.insert(0, str(ROOT / "rambdaA"))

from shadow_forward import SnapshotStore, PriceProvider  # noqa: E402
from signal_generator import DD_GUARD_TICKER  # noqa: E402

# 저장소 루트에 둔다(data/는 .gitignore). 이 원장은 shadow_ledger와 달리 **누적형**이라
# 매주 실행분이 append되어야 하며, 커밋되지 않으면 CI에서 표본이 영영 쌓이지 않는다.
LEDGER_PATH = ROOT / "signal_quality_ledger.json"

# ── STEP B 발동 기준 (사전 고정 — 사후에 바꾸지 않는다) ──
MIN_SAMPLE_WEEKS = 26      # 이 미만이면 어떤 판정도 하지 않는다
IC_MA_WINDOW = 26          # IC 이동평균 창
IC_NEGATIVE_STREAK = 4     # 이동평균이 음수인 주가 이만큼 연속되면 열화
# ── #OPEN-B 경보 임계 ──
PROXY_CORR_FLOOR = 0.80    # top10↔KODEX200 상관이 이 아래면 대리지표 가정 흔들림
PROXY_WINDOW = 60          # 상관·베타 산출 거래일


class SignalQualityAnalyzer:
    """두 스냅샷 사이의 순위 예측력을 계산한다(#OPEN-S)."""

    def __init__(self, prices: PriceProvider):
        self.prices = prices

    def evaluate(self, snap0: dict, snap1_date) -> dict | None:
        """snap0의 momentum 순위가 snap0.date→snap1_date 수익을 예측했는지.

        반환 None = 계산 불가(가격 결손 등). 조용히 0을 내지 않는다.
        """
        d0, d1 = snap0["date"], snap1_date
        rows = []
        for s in snap0["universe"]:
            p0 = self.prices.at(s["code"], d0)
            p1 = self.prices.at(s["code"], d1)
            mom = s.get("momentum")
            if p0 and p1 and p0 > 0 and mom is not None:
                rows.append({"mom": float(mom), "ret": p1 / p0 - 1})
        if len(rows) < 20:          # 표본이 얇으면 IC가 무의미
            return None

        df = pd.DataFrame(rows)
        # 순위상관(스피어만 동등) — scipy 의존 없이 rank 후 피어슨
        ic = float(df["mom"].rank().corr(df["ret"].rank()))
        if pd.isna(ic):
            return None

        df = df.sort_values("mom", ascending=False).reset_index(drop=True)
        uni_ret = float(df["ret"].mean())
        top10_ret = float(df.head(10)["ret"].mean())

        # 분위별 평균수익(Q1=최상위 모멘텀). 정상이면 단조 감소.
        q = pd.qcut(df["mom"].rank(ascending=False), 5, labels=False, duplicates="drop")
        quintiles = [round(float(g["ret"].mean()) * 100, 3)
                     for _, g in df.groupby(q, observed=True)]

        return {
            "n": len(df),
            "ic": round(ic, 4),
            "uni_pct": round(uni_ret * 100, 3),
            "top10_pct": round(top10_ret * 100, 3),
            "spread_pct": round((top10_ret - uni_ret) * 100, 3),
            "quintiles_pct": quintiles,
        }


class ProxyCorrelationMonitor:
    """DD가드 대리지표(KODEX200) 가정이 유효한지 감시한다(#OPEN-B)."""

    def __init__(self, prices: PriceProvider):
        self.prices = prices

    def measure(self, snap: dict, window: int = PROXY_WINDOW) -> dict | None:
        codes = [s["code"] for s in snap["universe"][:10]]   # momentum 상위 10
        series = [self.prices._s[c] for c in codes if c in self.prices._s]
        kd = self.prices.kodex()
        if len(series) < 5 or kd is None:
            return None

        asof = pd.Timestamp(snap["date"])
        pf = pd.concat(series, axis=1).sort_index()
        pf = pf[pf.index <= asof].dropna(how="all")
        pf_ret = pf.pct_change().dropna(how="all").mean(axis=1)
        kd_ret = kd[kd.index <= asof].pct_change().dropna()

        j = pd.concat([pf_ret.rename("pf"), kd_ret.rename("kd")], axis=1).dropna()
        j = j.tail(window)
        if len(j) < 20 or j["kd"].var() == 0:
            return None

        corr = float(j["pf"].corr(j["kd"]))
        beta = float(j["pf"].cov(j["kd"]) / j["kd"].var())
        if pd.isna(corr) or pd.isna(beta):
            return None
        return {"corr": round(corr, 4), "beta": round(beta, 4), "days": len(j)}


class SignalQualityLedger:
    """관측치 누적 원장. 판정(STEP B)은 사전 고정 기준으로만 내린다."""

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print("⚠️ 기존 원장 파손 → 새로 시작(백업 없음)")
        return {"records": []}

    def upsert(self, record: dict) -> None:
        """같은 구간이면 갱신(재실행 대비), 아니면 추가."""
        key = (record["from"], record["to"])
        for i, r in enumerate(self.data["records"]):
            if (r["from"], r["to"]) == key:
                self.data["records"][i] = record
                return
        self.data["records"].append(record)
        self.data["records"].sort(key=lambda r: r["to"])

    def verdict(self) -> dict:
        """STEP B — 최소 표본 전에는 판정하지 않는다."""
        recs = [r for r in self.data["records"] if r.get("ic") is not None]
        n = len(recs)
        if n < MIN_SAMPLE_WEEKS:
            return {"status": "INSUFFICIENT", "weeks": n,
                    "need": MIN_SAMPLE_WEEKS - n,
                    "note": f"판정 보류 — {MIN_SAMPLE_WEEKS}주 누적 전엔 경향만 본다"}

        s = pd.Series([r["ic"] for r in recs])
        ma = s.rolling(IC_MA_WINDOW).mean().dropna()
        streak = 0
        for v in reversed(ma.tolist()):
            if v < 0:
                streak += 1
            else:
                break
        spread_cum = sum(r["spread_pct"] for r in recs[-IC_MA_WINDOW:])

        degraded = (streak >= IC_NEGATIVE_STREAK) or (spread_cum < 0)
        return {
            "status": "DEGRADED" if degraded else "OK",
            "weeks": n,
            "ic_ma": round(float(ma.iloc[-1]), 4),
            "ic_ma_negative_streak": streak,
            "spread_cum_pct": round(spread_cum, 3),
            "note": ("발동 — STEP C(국면/구조 구분 → 섀도우 후보 검증)로. 프로덕션 직행 금지"
                     if degraded else "기준 미발동 → 아무것도 하지 않는다"),
        }

    def alerts(self) -> list[str]:
        """사용자에게 알릴 것만. 정상이면 빈 리스트(조용한 성공은 침묵)."""
        out = []
        v = self.verdict()
        if v["status"] == "DEGRADED":
            out.append(f"🚨 신호 열화 기준 발동 (IC 26주MA {v['ic_ma']:+.3f}, "
                       f"연속 {v['ic_ma_negative_streak']}주 / 스프레드누적 {v['spread_cum_pct']:+.2f}%p)")
        last = self.data["records"][-1] if self.data["records"] else None
        if last and last.get("proxy_corr") is not None and last["proxy_corr"] < PROXY_CORR_FLOOR:
            out.append(f"⚠️ 가드 대리지표 상관 저하: top10↔KODEX200 {last['proxy_corr']:.2f} "
                       f"(< {PROXY_CORR_FLOOR}) — DD가드가 포트 위험을 못 대변할 수 있음")
        return out

    def save(self) -> None:
        self.data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.data["verdict"] = self.verdict()
        self.data["criteria"] = {
            "min_sample_weeks": MIN_SAMPLE_WEEKS,
            "ic_ma_window": IC_MA_WINDOW,
            "ic_negative_streak": IC_NEGATIVE_STREAK,
            "proxy_corr_floor": PROXY_CORR_FLOOR,
            "note": "사전 고정 — 사후에 바꾸지 않는다(AUDIT.md ③ STEP B)",
        }
        self.data.setdefault("baseline_20260810", {
            "source": "74주 현유니버스 고정(생존편향 있음), 2026-08-10 수동 측정",
            "ic_mean": 0.064, "ic_median": 0.151, "ic_positive_ratio": 0.622,
            "ir": 0.160, "spread_mean_pct": 0.615, "spread_winrate": 0.554,
        })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def main() -> int:
    points, src = SnapshotStore().load()
    if src != "universe":
        print(f"⚠️ 스냅샷 소스가 '{src}' — 신호 품질은 universe(전체 유니버스+momentum)에서만 "
              f"측정 가능. 스킵.")
        return 0
    if len(points) < 2:
        print(f"⚠️ universe 스냅샷 {len(points)}개 → 구간 없음. 다음 주부터 측정 시작.")
        return 0

    codes = {s["code"] for p in points for s in p["universe"]}
    prices = PriceProvider().load(codes)
    analyzer = SignalQualityAnalyzer(prices)
    monitor = ProxyCorrelationMonitor(prices)
    ledger = SignalQualityLedger()

    print(f"📐 신호 품질 관측 — universe 스냅샷 {len(points)}개 "
          f"({points[0]['date']:%Y-%m-%d} ~ {points[-1]['date']:%Y-%m-%d})\n")
    print(f"{'구간':<24}{'n':>4}{'IC':>8}{'top10':>9}{'유니버스':>10}{'스프레드':>10}{'상관':>7}")

    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        res = analyzer.evaluate(a, b["date"])
        proxy = monitor.measure(a)
        rec = {
            "from": a["date"].strftime("%Y-%m-%d"),
            "to": b["date"].strftime("%Y-%m-%d"),
            "market_status": a["market"].get("market_status"),
            "ic": None, "top10_pct": None, "uni_pct": None,
            "spread_pct": None, "quintiles_pct": None, "n": None,
            "proxy_corr": proxy["corr"] if proxy else None,
            "proxy_beta": proxy["beta"] if proxy else None,
        }
        if res:
            rec.update(res)
            print(f"{rec['from']}→{rec['to']:<11}{res['n']:>4}{res['ic']:>+8.3f}"
                  f"{res['top10_pct']:>+8.2f}%{res['uni_pct']:>+9.2f}%"
                  f"{res['spread_pct']:>+9.2f}%p"
                  + (f"{proxy['corr']:>7.2f}" if proxy else f"{'—':>7}"))
        else:
            print(f"{rec['from']}→{rec['to']:<11}{'계산 불가(가격 결손/표본 부족)':>40}")
        ledger.upsert(rec)

    ledger.save()
    v = ledger.verdict()
    print(f"\n🧮 STEP B 판정: {v['status']} — {v['note']}")
    if v["status"] == "INSUFFICIENT":
        print(f"   누적 {v['weeks']}주 / 최소 {MIN_SAMPLE_WEEKS}주 (앞으로 {v['need']}주 더)")

    # BEAR 주 해석 주의: 예측이 틀려도 실제로는 매수하지 않아 손실이 나지 않는다
    bear = [r for r in ledger.data["records"] if r.get("market_status") == "BEAR"]
    if bear:
        print(f"   ※ BEAR 구간 {len(bear)}개 포함 — 그 주는 미매수라 IC가 음수여도 실현손실 아님")

    for a in ledger.alerts():
        print(f"\n{a}")
    print(f"\n💾 원장 저장: {LEDGER_PATH.relative_to(ROOT)}")
    print("⚠️ 이 수치로 파라미터를 튜닝하지 않는다 — STEP B 발동 + 섀도우 OOS 통과 시에만(AUDIT.md ③)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
